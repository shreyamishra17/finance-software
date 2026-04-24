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

        elif action == "wait_for_transactions":
            # Poll until transaction rows appear on the page — works for SPAs
            # where clicking an account doesn't trigger a full page navigation.
            timeout_ms = step.get("timeout", 300000)
            deadline = time.time() + timeout_ms / 1000
            poll_interval = 2
            found_sel = None
            poll_count = 0
            bank_key = self._cfg.get("bank_key", "bank")
            print("  [wait] Waiting for transactions — click your credit card account now…", flush=True)
            while time.time() < deadline:
                poll_count += 1
                try:
                    self._page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass

                # Every 10 polls (~20s) print diagnostics
                if poll_count % 10 == 1:
                    try:
                        url = self._page.url
                        print(f"  [wait] poll#{poll_count} url={url}", flush=True)
                    except Exception:
                        pass

                try:
                    found_sel = self._auto_discover_row_selector(timeout_ms=2000)
                except Exception as disc_exc:
                    print(f"  [wait] discovery error: {disc_exc}", flush=True)
                    found_sel = None

                if found_sel:
                    # Don't scrape the accounts overview — require a URL change away
                    # from the login/overview page AND at least a handful of rows.
                    try:
                        cur_url = self._page.url
                        row_count = len(self._page.query_selector_all(found_sel))
                    except Exception:
                        cur_url, row_count = "", 0

                    # Selectors that explicitly name "transaction" or "activity" are safe
                    # with just 1+ row; generic selectors need a higher bar.
                    is_txn_sel = any(k in found_sel.lower() for k in ("transaction", "activity", "purchase"))
                    min_rows = 1 if is_txn_sel else step.get("min_rows", 3)
                    on_login = any(k in cur_url for k in ("auth/signin", "auth/login", "login", "signin"))
                    print(f"  [wait] found sel={found_sel!r} rows={row_count} min={min_rows} url={cur_url}", flush=True)

                    if row_count >= min_rows and not on_login:
                        self._cfg.setdefault("transactions_config", {})["row_selector"] = found_sel
                        self._cfg["auto_discover_rows"] = False
                        print(f"  [wait] Confirmed {row_count} rows — starting scrape!", flush=True)
                        # Save a debug snapshot so we can inspect the see-more button etc.
                        try:
                            debug_dir = Path(__file__).resolve().parent.parent / "data" / "debug"
                            debug_dir.mkdir(parents=True, exist_ok=True)
                            snap = debug_dir / f"{bank_key}_confirmed.html"
                            snap.write_text(self._page.content(), encoding="utf-8")
                            print(f"  [wait] Page snapshot → {snap}", flush=True)
                        except Exception:
                            pass
                        self._update_banner("FIFO: Transactions found — scraping now…")
                        break
                    else:
                        print(f"  [wait] Skipping — on_login={on_login} rows={row_count} < min={min_rows}", flush=True)

                # Scroll down every 3 polls to trigger lazy-loaded transactions
                if poll_count % 3 == 0:
                    try:
                        cur_url = self._page.url
                        if not any(k in cur_url for k in ("auth/signin", "auth/login", "login", "signin")):
                            self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    except Exception:
                        pass

                time.sleep(poll_interval)

            if not found_sel:
                # Save screenshot + HTML for debugging
                debug_dir = Path(__file__).resolve().parent.parent / "data" / "debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                shot_path = debug_dir / f"{bank_key}_timeout.png"
                html_path = debug_dir / f"{bank_key}_timeout.html"
                try:
                    self._page.screenshot(path=str(shot_path), full_page=True)
                    print(f"  [wait] Screenshot: {shot_path}", flush=True)
                except Exception as e:
                    print(f"  [wait] Screenshot failed: {e}", flush=True)
                try:
                    html_path.write_text(self._page.content(), encoding="utf-8")
                    print(f"  [wait] HTML: {html_path}", flush=True)
                except Exception as e:
                    print(f"  [wait] HTML save failed: {e}", flush=True)
                try:
                    url = self._page.url
                except Exception:
                    url = "unknown"
                raise RuntimeError(
                    f"Timed out waiting for transaction rows.\n"
                    f"URL when timeout occurred: {url}\n"
                    f"Make sure you clicked on your specific credit card account.\n"
                    f"Debug files saved to {debug_dir}"
                )

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
        bank_key = self._cfg.get("bank_key", "unknown")
        first_url = (self._cfg.get("interactive_login_steps") or [{}])[0].get("url", "N/A")
        print(f"[run_scrape] bank_key={bank_key!r}  first_url={first_url!r}", flush=True)
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

            txn_cfg = self._cfg.get("transactions_config", {})
            configured_sel = txn_cfg.get("row_selector", "")

            if not configured_sel:
                # No selector yet — wait_for_transactions step should have set it.
                # If it didn't (bank config doesn't use that step), try discovery now.
                discovered = self._auto_discover_row_selector()
                if discovered:
                    self._cfg.setdefault("transactions_config", {})["row_selector"] = discovered
                else:
                    bank = self._cfg.get("bank_display_name", self._cfg.get("bank_key", "bank"))
                    raise RuntimeError(
                        f"{bank}: could not find any transaction rows on this page.\n"
                        f"Debug HTML saved to /tmp/{self._cfg.get('bank_key','bank')}_debug.html\n"
                        f"Make sure you clicked on a specific account and the transactions list is visible."
                    )
            else:
                # Configured selector exists — run transactions_steps as normal
                try:
                    self._run_steps(self._cfg.get("transactions_steps", []))
                except PWTimeoutError:
                    print(f"  Configured selector timed out, trying auto-discovery…")
                    discovered = self._auto_discover_row_selector()
                    if discovered:
                        self._cfg["transactions_config"]["row_selector"] = discovered
                    else:
                        bank = self._cfg.get("bank_display_name", self._cfg.get("bank_key", "bank"))
                        raise RuntimeError(
                            f"{bank}: could not find transaction rows on this page.\n"
                            f"Debug HTML saved to /tmp/{self._cfg.get('bank_key','bank')}_debug.html\n"
                            f"Make sure you navigated to the full transactions list."
                        ) from None

            # Capture URL before scraping — needed for headless replay later
            transactions_url = self._page.url
            if session_save_path:
                session_save_path.parent.mkdir(parents=True, exist_ok=True)
                self._context.storage_state(path=str(session_save_path))

            # Grab account balance from the page header (e.g. Capital One hero widget)
            page_balance = self._read_page_balance()

            self._update_banner("FIFO: Gathering your transactions — please keep this window open…")
            records = self._scrape_transactions(since_date)
            # Use the page-level balance if the row-level balance wasn't captured
            if page_balance and not self._ending_balance:
                self._ending_balance = page_balance
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
                    date_text = " ".join(_text(row, col_cfg.get("date", "")).split())
                    description = _text(row, col_cfg.get("description", ""))
                    amount_raw = _text(row, col_cfg.get("amount", ""))
                    # Strip any trailing junk after the dollar amount (e.g. Capital One
                    # appends "Shreya M. ...2365" after "$7.19" in the amount cell)
                    if amount_raw:
                        m = _AMOUNT_RE.search(amount_raw)
                        if m:
                            amount_raw = m.group(0)
                    # Capital One appends the category as the last word of the description
                    # e.g. "Whole Foods Market Grocery" → strip trailing known category words
                    if description:
                        _C1_CATS = {
                            "grocery","groceries","dining","healthcare","merchandise",
                            "automotive","entertainment","travel","gas","services",
                            "shopping","education","utilities","other","restaurant",
                            "transportation","airfare","hotel","pharmacy","personal",
                        }
                        parts = description.split()
                        if parts and parts[-1].lower() in _C1_CATS:
                            description = " ".join(parts[:-1]).strip()

                    # If CSS selectors returned nothing useful, fall back to regex on full text
                    if not (date_text and description and amount_raw):
                        full_text = (row.inner_text() or "").strip()
                        if full_text:
                            rx_date, rx_amount, rx_desc = _extract_from_row_text(full_text)
                            date_text = date_text or rx_date
                            amount_raw = amount_raw or rx_amount
                            description = description or rx_desc

                    if not date_text and not description and not amount_raw:
                        continue  # skip empty/header rows
                    # Skip rows that look like page-level accessibility blobs
                    # (Angular CDK sometimes injects aria-live rows with full page text)
                    if description and len(description) > 150:
                        continue

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
    # DOM auto-discovery
    # ------------------------------------------------------------------ #

    def _read_page_balance(self) -> str | None:
        """
        Try to read the current account balance from the page header (not transaction rows).
        Returns a formatted string like '$3,872.43' or None if not found.
        Used for credit card pages (Capital One) where the balance lives in the
        account hero section.
        """
        selectors = [
            # Capital One: the hero widget has two .cdk-visually-hidden spans —
            # the first is the balance, the second is available credit.
            # Target it specifically within the account-hero section.
            "c1-ease-account-details-top-middle-grid-cell .cdk-visually-hidden",
            "c1-ease-account-hero .cdk-visually-hidden",
            # Generic fallbacks
            "[class*='current-balance'] [class*='amount']",
            "[class*='account-balance'] [class*='amount']",
            "[class*='balance-amount']",
        ]
        for sel in selectors:
            try:
                el = self._page.query_selector(sel)  # first match only
                if el:
                    txt = (el.inner_text() or "").strip()
                    m = _AMOUNT_RE.search(txt)
                    if m:
                        raw = m.group(0)
                        try:
                            val = float(raw.replace("$", "").replace(",", ""))
                            if 0 < val < 1_000_000:
                                return raw
                        except ValueError:
                            pass
            except Exception:
                pass
        return None

    def _auto_discover_row_selector(self, timeout_ms: int = 12000) -> str | None:
        """
        Run JavaScript on the current page to find which CSS selector returns
        the most transaction-like rows. Tries known data-testid patterns first,
        then structural heuristics (li/div containing a date and dollar amount).

        Returns the winning selector string, or None if nothing found.
        Saves a debug HTML snapshot to /tmp/<bank_key>_debug.html on failure.
        """
        bank_key = self._cfg.get("bank_key", "bank")

        # Wait briefly for React to render
        try:
            self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

        result: dict = self._page.evaluate("""
        () => {
            const dollarRe = /\\$[\\d,]+\\.\\d{2}/;
            const dateRe = /\\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\\.?\\s+\\d{1,2}|\\d{1,2}\\/\\d{1,2}/i;

            // Strategy 1: known data-testid patterns and bank-specific selectors
            const knownSels = [
                // Capital One — c1-ease-row with table__row class (confirmed from debug HTML)
                "c1-ease-row.c1-ease-table__row",
                "[data-testid='purchase-list-item']",
                "[data-testid='transaction-list-item']",
                "[data-testid='txn-row']",
                "[data-testid='transaction-row']",
                "[data-testid='activity-row']",
                "li[class*='TransactionRow']",
                "tr[class*='transaction']",
                "div[class*='transaction-item']:not([class*='transaction-item__'])",
                "li[class*='transaction-item']:not([class*='transaction-item__'])",
            ];
            for (const sel of knownSels) {
                try {
                    const els = document.querySelectorAll(sel);
                    if (els.length >= 2) return { selector: sel, count: els.length, sample: els[0]?.outerHTML?.substring(0, 400) || "" };
                } catch(e) {}
            }

            // Strategy 2: find the TOPMOST element containing both a date and a dollar amount.
            // We normalise textContent (strip whitespace) before length-checking so Angular's
            // hidden aria-label / _ngcontent text doesn't falsely inflate the length.
            function normText(el) {
                return (el.textContent || "").replace(/\\s+/g, " ").trim();
            }

            const allEls = Array.from(document.querySelectorAll("li, tr, div"));
            const candidates = allEls.filter(el => {
                const txt = normText(el);
                return dollarRe.test(txt) && dateRe.test(txt) && txt.length < 2000 && txt.length > 10;
            });

            if (!candidates.length) return { selector: null, count: 0, sample: "", debug: "no candidates" };

            // For each candidate, walk UP to find the topmost ancestor that also qualifies
            function topmostMatch(el) {
                let top = el;
                let p = el.parentElement;
                while (p) {
                    const pt = normText(p);
                    if (pt.length > 2000) break;  // too large — definitely a container
                    // Stop if the parent contains multiple distinct transaction children
                    const pCount = Array.from(p.querySelectorAll(":scope > *")).filter(c => {
                        const ct = normText(c);
                        return dollarRe.test(ct) && dateRe.test(ct);
                    }).length;
                    if (pCount > 1) break;  // parent is a list container, not a single row
                    if (dollarRe.test(pt) && dateRe.test(pt)) top = p;
                    p = p.parentElement;
                }
                return top;
            }

            const topRows = candidates.map(topmostMatch);

            // Deduplicate (same element from multiple sub-element candidates)
            const seen = new WeakSet();
            const unique = topRows.filter(el => { if (seen.has(el)) return false; seen.add(el); return true; });

            if (!unique.length) return { selector: null, count: 0, sample: "", debug: "no unique rows after dedup" };

            // Build selector from the most common tag+class pattern among unique rows.
            // Use the BLOCK part of a BEM class (before __) and append :not([class*='__'])
            // so the selector never matches BEM sub-elements like transaction-item__amount.
            const freq = {};
            for (const el of unique) {
                const tag = el.tagName.toLowerCase();
                const rawCls = (el.className && typeof el.className === "string")
                    ? el.className.trim().split(/\\s+/).find(c => c && !c.startsWith("ng-") && c.length > 3)
                    : null;
                const blockCls = rawCls ? rawCls.split("__")[0] : null;
                const testid = el.getAttribute("data-testid");
                // Exclude BEM sub-elements in the selector itself
                const key = testid ? `[data-testid='${testid}']`
                           : blockCls ? `${tag}[class*='${blockCls}']:not([class*='${blockCls}__'])`
                           : tag;
                freq[key] = (freq[key] || 0) + 1;
            }
            const best = Object.entries(freq).sort((a,b) => b[1]-a[1])[0][0];

            // Verify the best selector actually matches the expected number of rows
            let finalCount = 0;
            try { finalCount = document.querySelectorAll(best).length; } catch(e) {}

            const sample = document.querySelector(best)?.outerHTML?.substring(0, 400) || "";
            return { selector: best, count: finalCount, unique: unique.length, sample, debug: "" };
        }
        """)

        sel = result.get("selector") if result else None
        count = result.get("count", 0) if result else 0

        if sel and count >= 1:
            print(f"  [discover] Found {count} rows with selector: {sel!r}")
            print(f"  [discover] Sample: {(result.get('sample') or '')[:200]}")
            return sel

        # Nothing found — dump HTML for debugging
        try:
            debug_dir = Path(__file__).resolve().parent.parent / "data" / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            html = self._page.content()
            debug_path = debug_dir / f"{bank_key}_debug.html"
            debug_path.write_text(html, encoding="utf-8")
            print(f"  [discover] No rows found. URL={self._page.url} HTML→{debug_path}", flush=True)
        except Exception as e:
            print(f"  [discover] Could not save debug HTML: {e}", flush=True)

        return None

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

        # Default to bundled Chromium. Some banks (e.g. Discover) fingerprint the browser
        # binary itself and reject "Chrome for Testing". Setting browser_channel="chrome"
        # in the bank config makes Playwright launch the real installed Chrome instead.
        engine = self._cfg.get("browser_engine", "chromium")
        channel = self._cfg.get("browser_channel", None)

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

        # WebKit uses Safari's UA automatically; real Chrome channel uses its own UA.
        # Only override for bundled Chromium (channel=None), which otherwise exposes
        # "Chrome for Testing" in its user agent string.
        if engine != "webkit" and channel is None:
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


_DATE_RE = re.compile(
    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?'
    r'|\d{1,2}/\d{1,2}(?:/\d{2,4})?'
    r'|\d{4}-\d{2}-\d{2}',
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r'[-+]?\$[\d,]+\.\d{2}')


def _extract_from_row_text(full_text: str) -> tuple[str, str, str]:
    """
    Extract (date, amount, description) from a row's full text content using regex.
    Used as a fallback when CSS column selectors are unavailable or wrong.
    """
    text = " ".join(full_text.split())  # normalise whitespace / newlines
    date_m = _DATE_RE.search(text)
    amount_m = _AMOUNT_RE.search(text)
    date = date_m.group(0).strip() if date_m else ""
    amount = amount_m.group(0).strip() if amount_m else ""
    # Description = text minus the date and amount tokens
    desc = text
    if date:
        desc = desc.replace(date_m.group(0), " ", 1)
    if amount:
        desc = desc.replace(amount_m.group(0), " ", 1)
    desc = re.sub(r'\s{2,}', ' ', desc).strip()
    return date, amount, desc


def _parse_date(text: str) -> datetime | None:
    """Parse a transaction date string. Returns None for unparseable text (e.g. 'Processing')."""
    text = " ".join(text.split())  # normalise newlines
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%b %d %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    # Try the month-name pattern without year (assume current year)
    m = re.match(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})$', text.strip(), re.I)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {datetime.now().year}", "%b %d %Y")
        except ValueError:
            pass
    return None  # "Processing" / pending rows → treat as current, always include


def _sanitize_filename(name: str) -> str:
    """Strip path components and disallowed characters from a bank-supplied filename."""
    name = Path(name).name
    name = re.sub(r"[^\w\-.]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "statement"
