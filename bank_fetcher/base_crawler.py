from __future__ import annotations

import platform
import plistlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)

from bank_fetcher.credential_manager import BankCredentials, generate_totp, get_credentials


class SessionExpiredError(RuntimeError):
    """Raised when a saved browser session is no longer valid."""


@dataclass
class TransactionRecord:
    date: str
    description: str
    amount: str       # raw string from page, e.g. "-$12.34"
    amount_type: str  # "debit" or "credit"
    balance: str = "" # running balance from the page, e.g. "$16,000.00"


@dataclass
class FetchResult:
    bank_key: str
    downloaded: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    months_requested: int = 0
    success: bool = False


def _detect_default_browser() -> tuple[str, str | None]:
    """
    Return (playwright_engine, channel) for the system's default browser.
    Reads the macOS LaunchServices plist directly — more reliable than parsing
    the text output of `defaults read`.
    Falls back to ('chromium', None) on any failure.
    """
    if platform.system() != "Darwin":
        return "chromium", None
    try:
        plist_path = (
            Path.home() / "Library" / "Preferences"
            / "com.apple.LaunchServices"
            / "com.apple.launchservices.secure.plist"
        )
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        found_https = False
        for handler in data.get("LSHandlers", []):
            if handler.get("LSHandlerURLScheme") != "https":
                continue
            found_https = True
            bundle = handler.get("LSHandlerRoleAll", "").lower()
            if "safari" in bundle:
                return "webkit", None
            if "chrome" in bundle:
                return "chromium", "chrome"
            if "firefox" in bundle:
                return "firefox", None
            if "edge" in bundle:
                return "chromium", "msedge"
            if "brave" in bundle:
                return "chromium", None
        # No explicit https handler = Safari is the macOS default
        if not found_https:
            return "webkit", None
    except Exception:
        pass
    return "chromium", None


class BankCrawler:
    """
    Single concrete crawler that executes step sequences defined in JSON config.
    No subclassing needed for new banks — just add a new *_fetcher_config.json.

    Config requires:
      login_steps      — list of step dicts to complete login + MFA
      statements_steps — list of step dicts to navigate to statement list
      download_config  — {row_selector, link_selector} for PDF download loop
    """

    def __init__(self, fetcher_cfg: dict, raw_dir: Path) -> None:
        self._cfg = fetcher_cfg
        self._raw_dir = raw_dir
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._creds: Optional[BankCredentials] = None
        self._ending_balance: str = ""   # balance from the most-recent (first) scraped row
        self._starting_balance: str = "" # balance from the oldest (last) scraped row

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def run(self, months: int = 3) -> FetchResult:
        """Full lifecycle: launch → login steps → statements steps → download → logout → close."""
        result = FetchResult(bank_key=self._cfg["bank_key"], months_requested=months)
        try:
            self._launch_browser()
            # Load credentials once into memory for the duration of this run
            self._creds = get_credentials(self._cfg)
            self._run_steps(self._cfg["login_steps"])
            self._run_steps(self._cfg["statements_steps"])
            result.downloaded = self._download_pdfs(months=months)
            result.success = True
        except Exception as exc:
            result.errors.append(str(exc))
        finally:
            self._creds = None  # clear credentials from memory
            self._safe_logout()
            self._close_browser()
        return result

    # ------------------------------------------------------------------ #
    # Step execution engine
    # ------------------------------------------------------------------ #

    def _run_steps(self, steps: list[dict]) -> None:
        """Execute each step dict in sequence."""
        for step in steps:
            self._execute_step(step)

    def _execute_step(self, step: dict) -> None:
        """Dispatch a single step by its action field."""
        action = step["action"]
        t = self._cfg["timeouts_ms"]

        if action == "goto":
            url = step["url"]
            if not url.startswith("https://"):
                raise ValueError(f"Insecure URL rejected in step config: {url!r}")
            self._page.goto(url, wait_until="domcontentloaded")

        elif action == "wait_for_selector":
            timeout = step.get("timeout", t["element_wait"])
            self._page.wait_for_selector(step["selector"], timeout=timeout)

        elif action == "fill":
            value = self._resolve_value(step["value"])
            self._page.fill(step["selector"], value)

        elif action == "click":
            self._page.click(step["selector"])

        elif action == "wait_for_url":
            timeout = step.get("timeout", t["page_load"])
            self._page.wait_for_url(step["pattern"], timeout=timeout)

        elif action == "wait_for_load_state":
            self._page.wait_for_load_state(step["state"], timeout=t["page_load"])

        elif action == "maybe_mfa":
            # Probe for MFA field; call _handle_mfa() if found, silently skip if absent
            probe_timeout = step.get("probe_timeout", 5000)
            try:
                self._page.wait_for_selector(step["probe_selector"], timeout=probe_timeout)
                self._handle_mfa()
            except PWTimeoutError:
                pass  # Trusted device or MFA not required this session

        elif action == "inject_banner":
            # Inject a visible guidance banner into the current page (non-blocking)
            msg = step.get("message", "")
            self._inject_banner(msg)

        elif action == "wait_for_navigation":
            # Wait for the next page navigation (any URL change triggered by user),
            # then wait for network to settle. Repeats networkidle wait to catch
            # pages that do a second AJAX load after initial HTML.
            timeout = step.get("timeout", t["page_load"])
            self._page.wait_for_event("framenavigated", timeout=timeout)
            self._page.wait_for_load_state("networkidle", timeout=t["page_load"])
            # Extra wait: BOA's Sparta widgets lazy-load via AJAX after networkidle
            time.sleep(6)
            self._page.wait_for_load_state("networkidle", timeout=t["page_load"])

        elif action == "sleep":
            time.sleep(step.get("seconds", 2))

        else:
            raise ValueError(
                f"Unknown step action: {action!r}. "
                f"Valid actions: goto, wait_for_selector, fill, click, "
                f"wait_for_url, wait_for_load_state, maybe_mfa, inject_banner, "
                f"wait_for_navigation, sleep"
            )

    def _inject_banner(self, message: str) -> None:
        """Inject (or replace) the FIFO guidance banner in the current page."""
        safe_msg = message.replace("'", "\\'")
        self._page.evaluate(
            f"""() => {{
                let b = document.getElementById('__fifo_banner__');
                if (!b) {{
                    b = document.createElement('div');
                    b.id = '__fifo_banner__';
                    b.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#1a6b3c;'
                        + 'color:#fff;padding:10px 16px;font-size:13px;font-family:sans-serif;'
                        + 'z-index:2147483647;text-align:center;box-shadow:0 2px 6px rgba(0,0,0,.3);';
                    document.body.prepend(b);
                }}
                b.textContent = '{safe_msg}';
            }}"""
        )

    def _update_banner(self, message: str) -> None:
        """Update banner text. Silent no-op if page is gone or banner doesn't exist."""
        try:
            self._inject_banner(message)
        except Exception:
            pass

    def _resolve_value(self, value: str) -> str:
        """Resolve $username / $password tokens from in-memory credentials."""
        if value == "$username":
            return self._creds.username
        if value == "$password":
            return self._creds.password
        return value

    # ------------------------------------------------------------------ #
    # Live transaction scraping (web page → structured records)
    # ------------------------------------------------------------------ #

    def run_scrape(
        self,
        since_date: datetime | None = None,
        session_save_path: Path | None = None,
    ) -> tuple[list[TransactionRecord], str | None]:
        """
        Login → navigate to transactions page → scrape all transactions since since_date.
        Returns (records, transactions_page_url).
        If session_save_path is provided, saves browser session state there for headless reuse
        and skips logout so the session stays valid.
        """
        transactions_url: str | None = None
        try:
            self._launch_browser()
            interactive_steps = self._cfg.get("interactive_login_steps")
            if interactive_steps:
                self._run_steps(interactive_steps)
            else:
                self._creds = get_credentials(self._cfg)
                self._run_steps(self._cfg["login_steps"])
            pages = self._context.pages
            if len(pages) > 1:
                self._page = pages[-1]
            try:
                self._run_steps(self._cfg.get("transactions_steps", []))
            except PWTimeoutError:
                bank = self._cfg.get("bank_display_name", self._cfg.get("bank_key", "bank"))
                row_sel = self._cfg.get("transactions_config", {}).get("row_selector", "")
                raise RuntimeError(
                    f"{bank}: could not find transaction rows on this page.\n"
                    f"Selector tried: {row_sel}\n"
                    f"Make sure you navigated to the full transactions list (not just the account overview). "
                    f"Run the DOM discovery script in the browser console to find the correct selector."
                ) from None

            # Capture URL before scraping — needed for headless replay later
            transactions_url = self._page.url
            if session_save_path:
                session_save_path.parent.mkdir(parents=True, exist_ok=True)
                self._context.storage_state(path=str(session_save_path))

            self._update_banner("FIFO: Gathering your transactions — please keep this window open…")
            records = self._scrape_transactions(since_date)
            self._update_banner(
                f"✓ Done! {len(records)} transaction{'s' if len(records) != 1 else ''} saved."
                " Closing window…"
            )
            return records, transactions_url
        finally:
            self._creds = None
            # Skip logout when session is saved — logout would invalidate the saved cookies
            if not session_save_path:
                self._safe_logout()
            self._close_browser()

    def run_scrape_headless(
        self,
        since_date: datetime | None = None,
        session_path: Path | None = None,
        account_url: str | None = None,
    ) -> list[TransactionRecord]:
        """
        Attempt a silent headless sync using a previously saved browser session.
        Raises SessionExpiredError if the session is no longer valid (redirected to login).
        No browser window is shown.
        """
        txn_cfg = self._cfg.get("transactions_config", {})
        row_sel = txn_cfg.get("row_selector", "tr.activity-row")
        try:
            self._launch_browser(headless=True, storage_state=session_path)
            if account_url:
                self._page.goto(account_url, wait_until="domcontentloaded")
            # Short timeout — if session expired, BOA redirects to login page
            try:
                self._page.wait_for_selector(row_sel, timeout=8000)
            except PWTimeoutError:
                current_url = self._page.url.lower()
                if any(kw in current_url for kw in ("login", "signin", "sign-in", "signon")):
                    raise SessionExpiredError(f"Redirected to login: {self._page.url}")
                raise SessionExpiredError("Transaction rows not found — session may have expired")
            return self._scrape_transactions(since_date)
        except SessionExpiredError:
            raise
        except Exception as exc:
            if any(kw in str(exc).lower() for kw in ("login", "signin", "expired", "unauthorized")):
                raise SessionExpiredError(str(exc)) from exc
            raise
        finally:
            self._creds = None
            # No logout — preserve session for next headless sync
            self._close_browser()

    def _scrape_transactions(self, since_date: datetime | None) -> list[TransactionRecord]:
        """
        Extract transactions using transactions_config selectors.
        Paginates by clicking view_more_selector until all rows within since_date are loaded.
        If since_date is None, fetches everything.
        """
        txn_cfg = self._cfg.get("transactions_config", {})
        row_sel = txn_cfg.get("row_selector", "")
        view_more_sel = txn_cfg.get("view_more_selector", "")
        col_cfg = txn_cfg.get("columns", {})

        if not row_sel:
            raise ValueError(
                "transactions_config.row_selector missing from config. "
                f"Add it to bank_fetcher/configs/{self._cfg['bank_key']}_fetcher_config.json"
            )

        records: list[TransactionRecord] = []
        done = False
        self._ending_balance = ""
        self._starting_balance = ""

        try:
            while not done:
                rows = self._page.query_selector_all(row_sel)
                # Only process rows we haven't seen yet
                new_rows = rows[len(records):]

                for row in new_rows:
                    date_text = _text(row, col_cfg.get("date", "")).strip()
                    description = _text(row, col_cfg.get("description", ""))
                    amount_raw = _text(row, col_cfg.get("amount", ""))

                    if not date_text and not description and not amount_raw:
                        continue  # skip empty/header rows

                    row_date = _parse_date(date_text)

                    # If this row is older than our cutoff, stop entirely
                    if since_date and row_date and row_date < since_date:
                        print(f"  Reached cutoff: {date_text} is before {since_date.date()} — stopping")
                        done = True
                        break

                    # Infer debit/credit from type column or amount sign
                    if col_cfg.get("amount_type"):
                        amount_type_text = _text(row, col_cfg["amount_type"]).lower()
                        # BOA column 3 shows human-readable labels like "Direct Deposit",
                        # "Zelle Credit", "Refund" — check for any deposit/credit keyword.
                        _CREDIT_KEYWORDS = {
                            "credit", "deposit", "refund", "reversal",
                            "interest paid", "reward", "cashback", "rebate",
                        }
                        if any(kw in amount_type_text for kw in _CREDIT_KEYWORDS):
                            amount_type = "credit"
                        elif "debit" in amount_type_text or "withdrawal" in amount_type_text or "purchase" in amount_type_text:
                            amount_type = "debit"
                        else:
                            # Column has a dollar amount or unknown text — fall back to sign
                            amount_type = "credit" if amount_raw.startswith("+") else "debit"
                    else:
                        amount_type = "credit" if amount_raw.startswith("+") else "debit"

                    balance_raw = _text(row, col_cfg.get("balance", ""))
                    record = TransactionRecord(
                        date=date_text,
                        description=description,
                        amount=amount_raw,
                        amount_type=amount_type,
                        balance=balance_raw,
                    )
                    records.append(record)

                    # First appended row = most recent transaction = ending balance
                    if len(records) == 1 and balance_raw:
                        self._ending_balance = balance_raw
                    # Always update starting_balance to the oldest row seen
                    if balance_raw:
                        self._starting_balance = balance_raw

                if done:
                    break

                # Try to load more rows: click "View More" button OR auto-scroll (infinite scroll)
                prev_count = len(rows)

                if view_more_sel:
                    view_more = self._page.query_selector(view_more_sel)
                    if view_more and view_more.is_visible():
                        print(f"  Loading more rows via button (have {len(records)} so far)…")
                        view_more.click()
                        self._page.wait_for_load_state("networkidle", timeout=15000)
                        time.sleep(1)
                        continue

                # No "View More" button found — try auto-scrolling for infinite scroll pages
                print(f"  Auto-scrolling to load more rows (have {len(records)} so far)…")
                self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2)
                try:
                    self._page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                new_count = len(self._page.query_selector_all(row_sel))
                if new_count <= prev_count:
                    break  # scroll loaded no new rows — we're done

        except Exception as exc:
            # If user closes the browser window mid-scrape, return whatever was collected.
            exc_msg = str(exc).lower()
            if any(kw in exc_msg for kw in ("closed", "detached", "destroyed", "crashed")):
                print(f"  Browser closed during scraping — saving {len(records)} partial records")
                return records
            raise

        print(f"  Scraped {len(records)} transactions total")
        return records

    # ------------------------------------------------------------------ #
    # PDF download loop
    # ------------------------------------------------------------------ #

    def _download_pdfs(self, months: int) -> list[Path]:
        """Download PDFs using selectors from download_config."""
        dl_cfg = self._cfg["download_config"]
        t = self._cfg["timeouts_ms"]
        account_hint = self._cfg.get("download_dir_hint", "")
        max_statements = min(months, self._cfg.get("max_statements", 12))
        saved: list[Path] = []

        rows = self._page.query_selector_all(dl_cfg["row_selector"])
        for row in rows[:max_statements]:
            link = row.query_selector(dl_cfg["link_selector"])
            if link is None:
                continue
            try:
                with self._page.expect_download(timeout=t["download_wait"]) as dl_info:
                    link.click()
                download = dl_info.value
                dest = self._dedupe_filename(download.suggested_filename, account_hint)
                download.save_as(str(dest))
                saved.append(dest)
                print(f"  Downloaded: {dest.name}")
            except Exception as exc:
                print(f"  Warning: failed to download one statement: {exc}")

        return saved

    # ------------------------------------------------------------------ #
    # MFA handler
    # ------------------------------------------------------------------ #

    def _handle_mfa(self) -> None:
        """Dispatch MFA based on mfa_mode config."""
        mode = self._cfg.get("mfa_mode", "otp_interactive")
        mfa_sel = self._cfg.get("mfa_selectors", {})
        t = self._cfg["timeouts_ms"]

        if mode == "none":
            return

        if mode == "totp":
            if not self._creds or not self._creds.totp_secret:
                raise RuntimeError(
                    "mfa_mode is 'totp' but no TOTP secret found in keychain. "
                    f"Re-run: python -m bank_fetcher setup --bank {self._cfg['bank_key']}"
                )
            otp_code = generate_totp(self._creds.totp_secret)
        else:  # otp_interactive
            bank_name = self._cfg["bank_display_name"]
            otp_code = input(f"\n[{bank_name}] Enter the OTP sent to your phone/email: ").strip()

        self._page.fill(mfa_sel["otp_field"], otp_code)
        self._page.click(mfa_sel["submit_button"])
        self._page.wait_for_load_state("networkidle", timeout=t["page_load"])

    # ------------------------------------------------------------------ #
    # Browser lifecycle
    # ------------------------------------------------------------------ #

    def _launch_browser(
        self,
        headless: bool | None = None,
        storage_state: Path | None = None,
    ) -> None:
        use_headless = headless if headless is not None else self._cfg.get("headless", False)
        self._pw = sync_playwright().start()

        # Always use bundled Chromium — reliable across both headless and interactive modes.
        engine, channel = "chromium", None

        ctx_kwargs: dict = dict(
            accept_downloads=True,
            viewport={"width": 1280, "height": 800},
        )
        if storage_state is not None:
            ctx_kwargs["storage_state"] = str(storage_state)

        print(f"  Browser: {engine}{f' ({channel})' if channel else ''}")

        # WebKit (Safari) and Firefox don't support the channel parameter.
        if engine == "webkit":
            try:
                self._browser = self._pw.webkit.launch(headless=use_headless)
            except Exception:
                raise RuntimeError(
                    "Safari (WebKit) is your default browser but Playwright's WebKit isn't installed.\n"
                    "Run this once to fix it:\n\n"
                    "    playwright install webkit\n"
                ) from None
        elif engine == "firefox":
            try:
                self._browser = self._pw.firefox.launch(headless=use_headless)
            except Exception:
                raise RuntimeError(
                    "Firefox is your default browser but Playwright's Firefox isn't installed.\n"
                    "Run this once to fix it:\n\n"
                    "    playwright install firefox\n"
                ) from None
        else:
            try:
                self._browser = self._pw.chromium.launch(
                    headless=use_headless,
                    channel=channel,
                    args=["--disable-extensions"],
                )
            except Exception:
                self._browser = self._pw.chromium.launch(
                    headless=use_headless,
                    args=["--disable-extensions"],
                )

        # WebKit uses Safari's user agent automatically; set one for Chromium/Firefox.
        if engine != "webkit":
            ctx_kwargs["user_agent"] = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )

        self._context = self._browser.new_context(**ctx_kwargs)
        self._page = self._context.new_page()

    def _safe_logout(self) -> None:
        try:
            logout_url = self._cfg.get("navigation", {}).get("logout_url")
            if logout_url and self._page:
                self._page.goto(logout_url, wait_until="domcontentloaded", timeout=10000)
        except Exception:
            pass

    def _close_browser(self) -> None:
        try:
            if self._context:
                self._context.clear_cookies()
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None

    # ------------------------------------------------------------------ #
    # Filename helpers
    # ------------------------------------------------------------------ #

    def _dedupe_filename(self, original_name: str, account_hint: str) -> Path:
        """
        Build a safe, deduplicated filename. Prevents path traversal from
        bank-supplied names. Appends timestamp suffix if file already exists.
        """
        safe_name = _sanitize_filename(original_name)
        bank_key = self._cfg["bank_key"]
        stem = Path(safe_name).stem
        canonical = f"{stem}_{bank_key}{account_hint}.pdf"
        dest = (self._raw_dir / canonical).resolve()

        # Path traversal guard
        if not str(dest).startswith(str(self._raw_dir.resolve())):
            raise ValueError(f"Suspicious download filename rejected: {original_name!r}")

        if dest.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = (self._raw_dir / f"{stem}_{bank_key}{account_hint}_{stamp}.pdf").resolve()

        return dest


def _text(element, selector: str) -> str:
    """Extract trimmed text from a child element, or empty string if not found."""
    if not selector:
        return (element.inner_text() or "").strip()
    child = element.query_selector(selector)
    if child is None:
        return ""
    return (child.inner_text() or "").strip()


def _parse_date(text: str) -> datetime | None:
    """Parse a transaction date string. Returns None for unparseable text (e.g. 'Processing')."""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None  # "Processing" / pending rows → treat as current, always include


def _sanitize_filename(name: str) -> str:
    """Strip path components and disallowed characters from a bank-supplied filename."""
    name = Path(name).name
    name = re.sub(r"[^\w\-.]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "statement"
