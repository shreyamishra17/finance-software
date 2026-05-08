from __future__ import annotations

import os
import platform
import plistlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
        self._ending_balance: str = ""    # balance from the most-recent (first) scraped row
        self._starting_balance: str = ""  # balance from the oldest (last) scraped row
        self._mfa_callback = None         # callable() → OTP string; set by run_scrape_automated
        self._is_automated: bool = False  # True during run_scrape_automated — disables input() blocking
        self._since_date: Optional[datetime] = None  # stored for use in step value resolution
        self._extra_values: dict[str, str] = {}  # arbitrary values captured via "capture" steps

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
            fallback_sel = step.get("fallback_selector", "")
            try:
                self._page.wait_for_selector(step["selector"], timeout=timeout)
            except PWTimeoutError:
                if fallback_sel:
                    print(f"  [wait_for_selector] '{step['selector']}' timed out, trying fallback '{fallback_sel}'…")
                    self._page.wait_for_selector(fallback_sel, timeout=timeout)
                else:
                    raise

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
            # Pre-seed with the configured row_selector so we check it first
            _preconfigured_sel = (self._cfg.get("transactions_config") or {}).get("row_selector", "")
            print(f"  [wait] Waiting for transaction rows (preconfigured: {_preconfigured_sel!r})…", flush=True)
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

                # Check the configured selector first (fast path — no JS evaluation needed)
                found_sel = None
                if _preconfigured_sel:
                    try:
                        count = len(self._page.query_selector_all(_preconfigured_sel))
                        if count >= 1:
                            found_sel = _preconfigured_sel
                            print(f"  [wait] preconfigured selector matched {count} rows", flush=True)
                    except Exception:
                        pass

                # Fall back to full auto-discovery if the configured selector didn't fire
                if not found_sel:
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

        elif action == "click_text":
            # Click the first element whose visible text matches `text` (case-insensitive).
            # Falls back to `fallback_text` then `fallback_selector` if text match times out.
            text = step["text"]
            timeout = step.get("timeout", t["element_wait"])
            fallback_text = step.get("fallback_text", "")
            fallback = step.get("fallback_selector", "")
            clicked = False
            try:
                # Playwright text selector — matches partial visible text
                self._page.click(f"text={text}", timeout=timeout)
                clicked = True
            except PWTimeoutError:
                pass
            if not clicked and fallback_text:
                try:
                    print(f"  [click_text] '{text}' not found, trying fallback_text '{fallback_text}'…")
                    self._page.click(f"text={fallback_text}", timeout=timeout)
                    clicked = True
                except PWTimeoutError:
                    pass
            if not clicked and fallback:
                print(f"  [click_text] text variants not found, trying fallback selector…")
                self._page.click(fallback, timeout=timeout)
                clicked = True
            if not clicked:
                raise RuntimeError(
                    f"Could not find clickable element with text '{text}'. "
                    f"The page may have loaded differently — check the ICICI statement page."
                ) from None

        elif action == "click_icase":
            # Case-insensitive click using JS innerText (CSS text-transform aware) to locate
            # the element, then page.mouse.click(x, y) with real mouse events so Angular's
            # zone-aware event handlers fire correctly.
            text_lower = step["text"].lower()
            exact = step.get("exact", False)
            sel_str = step.get("selectors", "button, [role='tab'], li, a, div, span")
            timeout_ms = step.get("timeout", t["element_wait"])
            max_text_len = step.get("max_text_len", len(text_lower) * 4)
            match_expr = (
                f"txt === {repr(text_lower)}" if exact
                else f"txt.includes({repr(text_lower)})"
            )
            js = f"""() => {{
                const candidates = Array.from(document.querySelectorAll({repr(sel_str)}));
                const results = [];
                for (const el of candidates) {{
                    // getBoundingClientRect works for position:fixed modals (offsetParent would be null)
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    if (r.bottom < 0 || r.top > window.innerHeight) continue;
                    const txt = (el.innerText || '').trim().toLowerCase();
                    if (txt.length === 0 || txt.length > {max_text_len}) continue;
                    if ({match_expr}) {{
                        results.push({{
                            x: Math.round(r.left + r.width / 2),
                            y: Math.round(r.top + r.height / 2),
                            text: (el.innerText || '').trim(),
                            tag: el.tagName.toLowerCase(),
                            area: r.width * r.height
                        }});
                    }}
                }}
                // Sort by area ascending — the most specific (smallest) element is the real target,
                // not a parent wrapper that happens to contain the same text.
                results.sort((a, b) => a.area - b.area);
                return results;
            }}"""
            deadline = time.time() + timeout_ms / 1000
            clicked = False
            while time.time() < deadline and not clicked:
                try:
                    matches = self._page.evaluate(js)
                    if matches:
                        # Log ALL candidates so mismatches are visible in the terminal
                        print(f"  [click_icase] '{step['text']}' candidates: "
                              f"{[m['text'] for m in matches]}")
                        # Prefer interactive elements (button/select/a) over generic containers
                        preferred = [m for m in matches if m.get("tag") in ("button", "select", "a", "input")]
                        coords = preferred[0] if preferred else matches[0]
                        print(f"  [click_icase] clicking '{coords['text']}' "
                              f"at ({coords['x']}, {coords['y']})")
                        self._page.mouse.click(coords["x"], coords["y"])
                        clicked = True
                    else:
                        time.sleep(0.4)
                except Exception as exc:
                    print(f"  [click_icase] error: {exc}")
                    time.sleep(0.4)
            if not clicked:
                raise RuntimeError(
                    f"click_icase: no visible element found whose innerText "
                    f"{'equals' if exact else 'contains'} '{step['text']}' "
                    f"(selectors={sel_str!r}, max_text_len={max_text_len})"
                )

        elif action == "wait_for_selector_any":
            # Wait until at least one of a list of selectors appears.
            # Useful when different page states show different elements.
            selectors = step["selectors"]  # list of CSS selector strings
            timeout_ms = step.get("timeout", t["element_wait"])
            deadline = time.time() + timeout_ms / 1000
            found = None
            while time.time() < deadline and not found:
                for sel in selectors:
                    try:
                        if self._page.query_selector(sel):
                            found = sel
                            break
                    except Exception:
                        pass
                if not found:
                    time.sleep(0.5)
            if not found:
                raise RuntimeError(
                    f"Timed out waiting for any of these selectors: {selectors}"
                )
            print(f"  [wait_any] Matched: {found!r}")

        elif action == "evaluate":
            # Run arbitrary JavaScript on the page.
            # Useful for date pickers that don't respond to `fill`.
            expression = step["expression"]
            # Support $since_date_dmy / $today_dmy tokens inside JS
            expression = expression.replace("$since_date_dmy", self._resolve_value("$since_date_dmy"))
            expression = expression.replace("$today_dmy", self._resolve_value("$today_dmy"))
            expression = expression.replace("$since_date_ymd", self._resolve_value("$since_date_ymd"))
            expression = expression.replace("$today_ymd", self._resolve_value("$today_ymd"))
            self._page.evaluate(expression)

        elif action == "fill_and_trigger":
            # Fill a field AND dispatch input/change events — needed for Angular's
            # reactive forms where `fill` alone doesn't trigger ngModel updates.
            # Supports comma-separated fallback selectors (tries each in order).
            value = self._resolve_value(step["value"])
            selectors = [s.strip() for s in step["selector"].split(",")]
            filled = False
            for sel in selectors:
                try:
                    el = self._page.query_selector(sel)
                    if el:
                        self._page.fill(sel, value)
                        self._page.dispatch_event(sel, "input")
                        self._page.dispatch_event(sel, "change")
                        filled = True
                        break
                except Exception:
                    continue
            if not filled:
                raise RuntimeError(
                    f"fill_and_trigger: could not find any field matching: {step['selector']}"
                )

        elif action == "capture":
            # Run JS on the page and store the string result in self._extra_values[key].
            # Silent no-op if JS throws or returns null/undefined — never breaks the scrape flow.
            key = step.get("key", "")
            js  = step.get("expression", "() => null")
            if key:
                try:
                    val = self._page.evaluate(js)
                    if val:
                        self._extra_values[key] = str(val).strip()
                        print(f"  [capture] {key} = {self._extra_values[key]!r}")
                    else:
                        print(f"  [capture] {key}: JS returned null/empty — skipping")
                except Exception as cap_exc:
                    print(f"  [capture] {key}: JS error — {cap_exc}")

        elif action == "sleep":
            time.sleep(step.get("seconds", 2))

        else:
            raise ValueError(
                f"Unknown step action: {action!r}. "
                f"Valid actions: goto, wait_for_selector, fill, fill_and_trigger, "
                f"click, click_text, click_icase, wait_for_selector_any, evaluate, "
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
                    b.style.cssText = 'position:fixed;top:0;left:0;right:0;'
                        + 'background:linear-gradient(90deg,#d4f0e8 0%,#fce4f0 100%);'
                        + 'color:#1f5f55;padding:0 16px;font-size:13px;font-weight:600;line-height:38px;'
                        + 'font-family:-apple-system,sans-serif;z-index:2147483647;text-align:center;'
                        + 'border-bottom:1.5px solid rgba(31,95,85,.15);'
                        + 'box-shadow:0 2px 12px rgba(31,95,85,.08);letter-spacing:.01em;';
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
        """Resolve $username / $password / $date tokens."""
        if value == "$username":
            return self._creds.username
        if value == "$password":
            return self._creds.password
        # Date tokens — use since_date if available, else fall back to 1 year ago
        _since = self._since_date or (datetime.now() - timedelta(days=365))
        _today = datetime.now()
        if value == "$since_date_dmy":   return _since.strftime("%d/%m/%Y")
        if value == "$today_dmy":        return _today.strftime("%d/%m/%Y")
        if value == "$since_date_mdy":   return _since.strftime("%m/%d/%Y")
        if value == "$today_mdy":        return _today.strftime("%m/%d/%Y")
        if value == "$since_date_ymd":   return _since.strftime("%Y-%m-%d")
        if value == "$today_ymd":        return _today.strftime("%Y-%m-%d")
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
        self._since_date = since_date  # store for $since_date_* token resolution in steps
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

            # Always run transactions_steps after login (may be empty for BOA-style banks
            # that use wait_for_transactions, or a full navigation sequence for ICICI-style banks).
            txn_steps = self._cfg.get("transactions_steps", [])
            if txn_steps:
                try:
                    self._run_steps(txn_steps)
                except (PWTimeoutError, RuntimeError) as e:
                    # Navigation steps may timeout or fail on the final wait_for_selector
                    # (Angular re-render instability). Log but continue — the scraper
                    # will attempt to find rows on whatever page is currently loaded.
                    print(f"  [warn] transactions_steps did not complete cleanly: {e}")
                    print(f"  Continuing — will attempt scraping on current page.")

            txn_cfg = self._cfg.get("transactions_config", {})
            configured_sel = txn_cfg.get("row_selector", "")

            if not configured_sel:
                # No selector yet — wait_for_transactions step should have set it,
                # or transactions_steps should have navigated to a scrappable page.
                discovered = self._auto_discover_row_selector()
                if discovered:
                    self._cfg.setdefault("transactions_config", {})["row_selector"] = discovered
                else:
                    bank = self._cfg.get("bank_display_name", self._cfg.get("bank_key", "bank"))
                    raise RuntimeError(
                        f"{bank}: could not find any transaction rows on this page.\n"
                        f"Debug HTML saved to data/debug/{self._cfg.get('bank_key','bank')}_debug.html\n"
                        f"Make sure you clicked on a specific account and the transactions list is visible."
                    )

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

    def run_scrape_automated(
        self,
        username: str,
        password: str,
        mfa_callback=None,
        since_date: datetime | None = None,
        session_save_path: Path | None = None,
    ) -> tuple[list[TransactionRecord], str | None]:
        """
        Headless automated scrape using explicit credentials (Smart Connect / server-side).
        Uses login_steps from the config — skips interactive_login_steps entirely.
        mfa_callback: callable that blocks until the user submits an OTP via the web UI,
                      then returns it as a string. Required when mfa_mode='otp_interactive'.
        If session_save_path provided, saves fresh session cookies there for next headless reuse.
        """
        from bank_fetcher.credential_manager import BankCredentials
        login_steps = self._cfg.get("login_steps")
        if not login_steps:
            bank = self._cfg.get("bank_display_name", self._cfg.get("bank_key", "this bank"))
            raise RuntimeError(
                f"Smart Connect is not yet configured for {bank}. "
                "The bank's fetcher config needs a 'login_steps' section."
            )

        self._creds = BankCredentials(username=username, password=password)
        self._mfa_callback = mfa_callback
        self._is_automated = True         # prevents input() blocking during MFA
        self._since_date = since_date  # store for $since_date_* token resolution
        transactions_url: str | None = None

        try:
            self._launch_browser(headless=True)
            self._run_steps(login_steps)

            # Discover or use configured row selector
            txn_cfg = self._cfg.get("transactions_config", {})
            if not txn_cfg.get("row_selector"):
                discovered = self._auto_discover_row_selector()
                if discovered:
                    self._cfg.setdefault("transactions_config", {})["row_selector"] = discovered
                else:
                    raise RuntimeError("Could not find transaction rows on the page.")
            else:
                self._run_steps(self._cfg.get("transactions_steps", []))

            transactions_url = self._page.url

            # Save fresh session cookies before scraping so next headless sync can reuse them
            if session_save_path:
                session_save_path.parent.mkdir(parents=True, exist_ok=True)
                self._context.storage_state(path=str(session_save_path))

            page_balance = self._read_page_balance()
            records = self._scrape_transactions(since_date)
            if page_balance and not self._ending_balance:
                self._ending_balance = page_balance
            return records, transactions_url
        finally:
            self._creds = None
            self._mfa_callback = None
            self._is_automated = False
            # Skip logout when saving session — logout invalidates the cookies we just saved
            if not session_save_path:
                self._safe_logout()
            self._close_browser()

    def _scrape_transactions(self, since_date: datetime | None) -> list[TransactionRecord]:
        """
        Extract transactions using transactions_config selectors.

        pagination_style (in transactions_config):
          "scroll"     — default; rows accumulate via infinite scroll or "Load More" button.
          "next_page"  — each page REPLACES rows (e.g. ICICI Detailed Statement).
                         Clicks view_more_selector to advance pages until cutoff is reached
                         or the Next button is disabled/gone.
        """
        txn_cfg = self._cfg.get("transactions_config", {})
        row_sel = txn_cfg.get("row_selector", "")
        view_more_sel = txn_cfg.get("view_more_selector", "")
        col_cfg = txn_cfg.get("columns", {})
        pagination_style = txn_cfg.get("pagination_style", "scroll")

        if not row_sel:
            raise ValueError(
                "transactions_config.row_selector missing from config. "
                f"Add it to bank_fetcher/configs/{self._cfg['bank_key']}_fetcher_config.json"
            )

        records: list[TransactionRecord] = []
        seen_keys: set[str] = set()   # dedup guard for next_page mode
        done = False
        self._ending_balance = ""
        self._starting_balance = ""
        # ── Grouped-date tracking ──────────────────────────────────────────────
        # Some banks (e.g. ICICI Detailed Statement) display a date-only header
        # row above each group of transactions instead of repeating the date in
        # every transaction row.  We track the most-recently-seen date so we can
        # assign it to the transaction rows that follow.
        _current_group_date: str = ""

        _html_dump_done = False   # only dump once per scrape session

        try:
            while not done:
                rows = self._page.query_selector_all(row_sel)
                # "next_page" mode: each page replaces rows — process ALL rows every iteration.
                # "scroll" mode: rows accumulate — only process the newly added tail.
                new_rows = rows if pagination_style == "next_page" else rows[len(records):]
                records_before_page = len(records)   # for stuck-page detection

                # ── One-time HTML dump for date-cell debugging ────────────────
                # Saves the first 20 rows' raw HTML plus the parent/sibling context
                # of the first row so we can find where the date lives in the DOM.
                if not _html_dump_done and new_rows:
                    try:
                        _debug_html = self._page.evaluate(f"""() => {{
                            const rows = Array.from(document.querySelectorAll({repr(row_sel)})).slice(0, 20);
                            const rowDump = rows.map((r, i) => `<!-- ROW ${{i}} -->\\n` + r.outerHTML).join('\\n\\n');

                            // Walk up from first row and dump the ancestor chain
                            // (up to 6 levels) plus each ancestor's outer HTML (first 2000 chars)
                            // so we can see where dates are placed in the page structure.
                            let contextDump = '\\n\\n<!-- === ANCESTOR CONTEXT (from first row) === -->';
                            const firstRow = rows[0];
                            if (firstRow) {{
                                let node = firstRow.parentElement;
                                for (let lvl = 0; lvl < 7 && node && node.tagName !== 'BODY'; lvl++) {{
                                    const tag = node.tagName + (node.className ? '.' + (node.className.toString().trim().split(/\\s+/).join('.')) : '');
                                    const snippet = node.outerHTML.slice(0, 3000);
                                    contextDump += `\\n\\n<!-- ANCESTOR[${{lvl}}] ${{tag}} -->\\n${{snippet}}${{node.outerHTML.length > 3000 ? '\\n... [truncated at 3000 chars]' : ''}}`;
                                    node = node.parentElement;
                                }}
                            }}
                            return rowDump + contextDump;
                        }}""")
                        _debug_path = Path(__file__).resolve().parent.parent / "data" / "debug" / "icici_rows_debug.html"
                        _debug_path.parent.mkdir(parents=True, exist_ok=True)
                        _debug_path.write_text(_debug_html or "", encoding="utf-8")
                        print(f"  [debug] Row HTML dumped → {_debug_path}")
                    except Exception as _de:
                        print(f"  [debug] HTML dump failed: {_de}")
                    _html_dump_done = True

                _diag_row_count = 0  # reset per page
                for row in new_rows:
                    # ── Date extraction ─────────────────────────────────────────
                    # If the config has an explicit date selector, use it.
                    # If it's "" (empty), the bank has NO date column in each row
                    # (e.g. ICICI groups rows by date with a header outside <tbody>).
                    # In that case we leave date_text="" and let group-header tracking
                    # or the DOM-context JS finder fill it in below.
                    _date_selector = col_cfg.get("date", "")
                    if _date_selector:
                        _raw_date_cell = _text(row, _date_selector)
                        date_text = " ".join(_raw_date_cell.split())
                        # textContent fallback — catches dates hidden by CSS visibility
                        if not date_text:
                            try:
                                _tc = row.evaluate(f"""el => {{
                                    const cell = el.querySelector({repr(_date_selector)});
                                    return cell ? cell.textContent.trim() : '';
                                }}""") or ""
                                date_text = " ".join(_tc.split())
                            except Exception:
                                pass
                    else:
                        date_text = ""  # No date column — will be filled from group header or DOM

                    # Diagnostic: log first 3 rows per page
                    _diag_row_count += 1
                    if _diag_row_count <= 3:
                        _date_sel = col_cfg.get("date", "")
                        _d1 = _text(row, _date_sel) if _date_sel else "(no date col)"
                        _d2 = _text(row, col_cfg.get("description", ""))
                        _d3 = _text(row, col_cfg.get("credit_amount", ""))
                        _d4 = _text(row, col_cfg.get("debit_amount", ""))
                        _d5 = _text(row, col_cfg.get("balance", ""))
                        print(f"  [diag row {_diag_row_count}] date={_d1!r:20} desc={_d2!r:40} "
                              f"credit={_d3!r:12} debit={_d4!r:12} bal={_d5!r:12}")

                    description = _text(row, col_cfg.get("description", ""))

                    # ── Split debit/credit columns (Indian banks: ICICI, HDFC, etc.) ──
                    # When debit_amount + credit_amount are configured instead of amount,
                    # read both and use whichever is non-empty to set amount and type.
                    _EMPTY_VALS = {"", "-", "–", "—", "0", "0.00", "nil", "n/a"}
                    amount_type: str = "debit"
                    if col_cfg.get("debit_amount") or col_cfg.get("credit_amount"):
                        debit_raw  = _text(row, col_cfg.get("debit_amount",  "")).strip()
                        credit_raw = _text(row, col_cfg.get("credit_amount", "")).strip()
                        # Extract numeric portion from each (may contain ₹/$, commas, or plain decimals)
                        debit_clean  = _extract_amount_str(debit_raw)
                        credit_clean = _extract_amount_str(credit_raw)
                        d_empty = debit_clean.replace(",", "").replace(".", "").strip("0") == ""
                        c_empty = credit_clean.replace(",", "").replace(".", "").strip("0") == ""
                        if not d_empty and d_empty == c_empty:
                            # Both non-empty (rare edge case): treat as debit
                            amount_raw = debit_clean
                            amount_type = "debit"
                        elif not d_empty:
                            amount_raw = debit_clean
                            amount_type = "debit"
                        elif not c_empty:
                            amount_raw = credit_clean
                            amount_type = "credit"
                        else:
                            amount_raw = ""   # skip — both columns blank (header/spacer row)
                    else:
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
                            # Only inherit rx_date if the config has an explicit date column.
                            # When _date_selector is "" the date lives OUTSIDE the row (e.g. ICICI),
                            # so rx_date would be a bogus match from a transaction reference number.
                            if _date_selector:
                                date_text = date_text or rx_date
                            amount_raw = amount_raw or rx_amount
                            description = description or rx_desc

                    if not date_text and not description and not amount_raw:
                        continue  # skip empty/header rows
                    # Skip rows that look like page-level accessibility blobs
                    # (Angular CDK sometimes injects aria-live rows with full page text)
                    if description and len(description) > 150:
                        continue

                    # ── Grouped-date rows (ICICI-style) ────────────────────────
                    # Case A: date selector returned text but amount is empty
                    #         → this IS a date-group header row; capture and skip.
                    # Case B: no amount AND no description → spacer / header row;
                    #         try to parse full row text as a date then skip.
                    if (date_text and not amount_raw) or (not amount_raw and not description):
                        _hdr_candidate = date_text or " ".join((row.inner_text() or "").split())
                        _parsed_hdr = _parse_date(_hdr_candidate)
                        if _parsed_hdr:
                            _current_group_date = _parsed_hdr.strftime("%Y-%m-%d")
                            print(f"  [date-group header] {_hdr_candidate!r} → {_current_group_date}")
                        continue  # skip all non-transaction rows regardless

                    # If this transaction row still has no date, try two strategies:
                    # 1. JS DOM-context walk: scan sibling/ancestor elements for a date string.
                    #    Re-run for every row so we detect date-group changes mid-page.
                    #    (Used when dates live OUTSIDE the <table>, e.g. ICICI Angular SPA.)
                    # 2. Fall back to the most-recently-seen group date.
                    if not date_text:
                        if not _date_selector:
                            # DOM-context finder — only for banks with no date column.
                            #
                            # ICICI-specific: each date-group is wrapped in div.transaction
                            # which contains div.date > p.date-1 (day) + p.date-2 (month).
                            # The year lives in the nearest preceding div.date-transaction-top
                            # sibling (p.date = "April 2026").
                            #
                            # Generic fallback: walk up the ancestor chain checking previous
                            # siblings for a "DD Mon YYYY" formatted string.
                            try:
                                _dom_date = row.evaluate(r"""el => {
                                    // ── ICICI-specific path ────────────────────────────
                                    const txnDiv = el.closest('div.transaction');
                                    if (txnDiv) {
                                        const dayEl = txnDiv.querySelector('.date-1');
                                        const monEl = txnDiv.querySelector('.date-2');
                                        const d = dayEl ? dayEl.textContent.trim() : '';
                                        const mo = monEl ? monEl.textContent.trim() : '';
                                        let year = '';
                                        // Year is in div.date-transaction-top.  Search up
                                        // the ancestor chain (not just immediate parent) because
                                        // on paginated pages the wrapper depth can vary.
                                        let searchNode = txnDiv.parentElement;
                                        while (searchNode && searchNode.tagName !== 'BODY' && !year) {
                                            const hdr = searchNode.querySelector('.date-transaction-top .date');
                                            if (hdr) {
                                                const ym = (hdr.textContent || '').match(/\d{4}/);
                                                if (ym) year = ym[0];
                                            }
                                            searchNode = searchNode.parentElement;
                                        }
                                        if (d && mo) {
                                            if (!year) {
                                                // Smart fallback: months numerically > current month
                                                // must be from LAST year (e.g. Dec when today is May).
                                                const MONS = {jan:1,feb:2,mar:3,apr:4,may:5,jun:6,
                                                              jul:7,aug:8,sep:9,oct:10,nov:11,dec:12};
                                                const moNum = MONS[mo.toLowerCase().substring(0,3)] || 0;
                                                const curMon = new Date().getMonth() + 1;
                                                const curYr  = new Date().getFullYear();
                                                year = String(moNum > curMon ? curYr - 1 : curYr);
                                            }
                                            return `${d} ${mo} ${year}`;
                                        }
                                    }

                                    // ── Generic fallback for other banks ──────────────
                                    const dateRe = /^\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}$/i;
                                    const looseRe = /\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}\b/i;
                                    let node = el;
                                    for (let depth = 0; depth < 10; depth++) {
                                        let sib = node.previousElementSibling;
                                        for (let s = 0; s < 15 && sib; s++) {
                                            const t = (sib.innerText || sib.textContent || '').trim();
                                            if (dateRe.test(t)) return t;
                                            const leaf = sib.querySelector('p, span, div, td');
                                            if (leaf) {
                                                const lt = (leaf.innerText || leaf.textContent || '').trim();
                                                if (dateRe.test(lt)) return lt;
                                                const m = looseRe.exec(lt);
                                                if (m) return m[0];
                                            }
                                            sib = sib.previousElementSibling;
                                        }
                                        node = node.parentElement;
                                        if (!node || node.tagName === 'BODY') break;
                                    }
                                    return null;
                                }""")
                                if _dom_date:
                                    _parsed_dom = _parse_date(_dom_date.strip())
                                    if _parsed_dom:
                                        _new_grp = _parsed_dom.strftime("%Y-%m-%d")
                                        if _new_grp != _current_group_date:
                                            print(f"  [dom-date] {_dom_date!r} → {_new_grp}")
                                            _current_group_date = _new_grp
                            except Exception:
                                pass
                        # Use cached group date (updated by DOM finder above, or by in-row header)
                        if _current_group_date:
                            date_text = _current_group_date

                    row_date = _parse_date(date_text)

                    # If this row is older than our cutoff, stop entirely
                    if since_date and row_date and row_date < since_date:
                        print(f"  Reached cutoff: {date_text} is before {since_date.date()} — stopping")
                        done = True
                        break

                    # Infer debit/credit from type column or amount sign.
                    # Skip if already determined by the split debit/credit column path above.
                    if not (col_cfg.get("debit_amount") or col_cfg.get("credit_amount")) and col_cfg.get("amount_type"):
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
                    elif not (col_cfg.get("debit_amount") or col_cfg.get("credit_amount")):
                        # Sign-based fallback — only when no split columns and no type column
                        amount_type = "credit" if amount_raw.startswith("+") else "debit"

                    balance_raw = _text(row, col_cfg.get("balance", ""))
                    record = TransactionRecord(
                        date=row_date.strftime("%Y-%m-%d") if row_date else date_text,
                        description=description,
                        amount=amount_raw,
                        amount_type=amount_type,
                        balance=balance_raw,
                    )

                    # In next_page mode pages replace each other, so the same row can appear
                    # each iteration if pagination gets stuck.  Skip already-seen rows.
                    if pagination_style == "next_page":
                        row_key = f"{record.date}|{record.description}|{record.amount}|{record.balance}"
                        if row_key in seen_keys:
                            continue
                        seen_keys.add(row_key)

                    records.append(record)

                    # First appended row = most recent transaction = ending balance
                    if len(records) == 1 and balance_raw:
                        self._ending_balance = balance_raw
                    # Always update starting_balance to the oldest row seen
                    if balance_raw:
                        self._starting_balance = balance_raw

                if done:
                    break

                # ── Pagination ─────────────────────────────────────────────────
                if pagination_style == "next_page":
                    # Stuck-page guard: if every row on this page was already in seen_keys
                    # (i.e. new_rows added 0 new records), the Next button click did nothing
                    # useful — stop to avoid an infinite loop.
                    records_before = len(records)  # captured just before the loop above ran

                    # Find and click the Next button using JS + mouse.click() so Angular's
                    # event handlers fire correctly. Strategy:
                    # 1. Try CSS view_more_selector (works for standard banks)
                    # 2. Fall back to JS that finds the button AFTER "Page X of Y" text
                    #    (works for ICICI's custom Angular pagination component)
                    next_coords = None

                    # Strategy 1: CSS selector
                    if view_more_sel:
                        next_btn = self._page.query_selector(view_more_sel)
                        if next_btn and next_btn.is_visible():
                            disabled = next_btn.get_attribute("disabled")
                            aria_disabled = next_btn.get_attribute("aria-disabled")
                            btn_class = next_btn.get_attribute("class") or ""
                            if (disabled is not None
                                    or aria_disabled == "true"
                                    or "disabled" in btn_class
                                    or "inactive" in btn_class):
                                print(f"  [next_page] Next button disabled — last page ({len(records)} total)")
                                break
                            r = next_btn.bounding_box()
                            if r:
                                next_coords = {"x": r["x"] + r["width"] / 2,
                                               "y": r["y"] + r["height"] / 2}

                    # Strategy 2: JS — find "Page X of Y" anywhere in the DOM (it may be
                    # below the initial viewport on long ICICI statement pages), scroll it
                    # into view, then find the > button using vertical proximity so we never
                    # accidentally click a date-filter button that happens to be nearby.
                    if next_coords is None:
                        # Step A: scroll the pagination bar into view
                        self._page.evaluate("""() => {
                            for (const el of document.querySelectorAll('*')) {
                                const t = (el.innerText || '').trim();
                                if (/^page\\s+\\d+\\s+of\\s+\\d+$/i.test(t)) {
                                    el.scrollIntoView({block: 'center', inline: 'nearest'});
                                    return;
                                }
                            }
                            window.scrollTo(0, document.body.scrollHeight);
                        }""")
                        import time as _time; _time.sleep(0.6)

                        # Step B: dump every leaf element near the pagination row so we can
                        # see exactly what ICICI renders for the > arrow button.
                        _dbg = self._page.evaluate("""() => {
                            let pageEl = null, best = 999;
                            for (const el of document.querySelectorAll('*')) {
                                const t = (el.innerText || '').trim();
                                if (!/^page\\s+\\d+\\s+of\\s+\\d+$/i.test(t)) continue;
                                const r = el.getBoundingClientRect();
                                if (r.width === 0 && r.height === 0) continue;
                                if (el.children.length < best) { pageEl = el; best = el.children.length; }
                            }
                            if (!pageEl) return 'NO_PAGE_TEXT';
                            const pr = pageEl.getBoundingClientRect();
                            const cy = pr.top + pr.height / 2;
                            const all = Array.from(document.querySelectorAll('*'))
                                .filter(el => {
                                    const r = el.getBoundingClientRect();
                                    return Math.abs((r.top + r.height / 2) - cy) < 55;
                                })
                                .map(el => {
                                    const r = el.getBoundingClientRect();
                                    const txt = (el.innerText || '').trim().slice(0, 20);
                                    const cls = (el.className || '').toString().slice(0, 40);
                                    const aria = el.getAttribute('aria-label') || '';
                                    return `<${el.tagName}[${el.children.length}ch] cls="${cls}" aria="${aria}" txt="${txt}" x=${Math.round(r.left)} w=${Math.round(r.width)} h=${Math.round(r.height)}>`;
                                });
                            return `pageY=${Math.round(cy)} pageRight=${Math.round(pr.right)} | ` + all.join('\\n  ');
                        }""")
                        print(f"  [pagination-debug]\n  {_dbg}")

                        next_coords = self._page.evaluate("""() => {
                            // 1. Find the "Page N of M" element — anywhere in DOM, no viewport filter.
                            //    Prefer the smallest (leaf-like) element to avoid matching a wrapper
                            //    whose innerText happens to include the page text among other content.
                            let pageEl = null;
                            for (const el of document.querySelectorAll('*')) {
                                const t = (el.innerText || '').trim();
                                if (!/^page\\s+\\d+\\s+of\\s+\\d+$/i.test(t)) continue;
                                const r = el.getBoundingClientRect();
                                if (r.width === 0 || r.height === 0) continue;
                                // Pick element with fewest children (most specific)
                                if (!pageEl || el.children.length < pageEl.children.length) {
                                    pageEl = el;
                                }
                            }
                            if (!pageEl) return {error: 'page_text_not_found'};

                            // 2. Check if on last page
                            const m = (pageEl.innerText || '').match(/page\\s+(\\d+)\\s+of\\s+(\\d+)/i);
                            if (m && parseInt(m[1]) >= parseInt(m[2])) return {last: true};

                            // 3. Find the > button by vertical proximity to the page text.
                            //    After scrollIntoView the page text should be on-screen.
                            //    The real pagination > button is on the SAME row (within 40px
                            //    vertically) AND to the right of the page text.
                            //    Date/filter buttons (date-cta class) are excluded explicitly.
                            const pageRect = pageEl.getBoundingClientRect();
                            const pageCenterY = pageRect.top + pageRect.height / 2;

                            // ICICI renders pagination arrows as inline SVG elements (not
                            // buttons or links). Must include svg in the selector.
                            const SELECTORS = 'button, [role="button"], a, li, span, svg';
                            // Text that definitively identifies download/export buttons (not nav arrows)
                            const SKIP_TEXTS = ['download', 'pdf', 'xls', 'xlsx', 'csv', 'export', 'print'];
                            const btns = Array.from(
                                document.querySelectorAll(SELECTORS)
                            ).filter(b => {
                                const r = b.getBoundingClientRect();
                                if (r.width === 0 || r.height === 0) return false;
                                const btnCenterY = r.top + r.height / 2;
                                // Must be on the same horizontal row as the page text (within 40px)
                                if (Math.abs(btnCenterY - pageCenterY) > 40) return false;
                                // Must be to the RIGHT of the page text
                                if (r.left < pageRect.right - 5) return false;
                                const cls = (b.className || '').toString().toLowerCase();
                                const txt = (b.innerText || '').trim().toLowerCase();
                                // Exclude known CTA/download button classes
                                if (cls.includes('blue-cta') || cls.includes('date-cta') ||
                                    cls.includes('datepicker') || cls.includes('calendar')) return false;
                                // Exclude elements whose visible text is a download/export label
                                if (SKIP_TEXTS.some(s => txt.includes(s))) return false;
                                return true;
                            });

                            if (btns.length === 0) {
                                // Debug: report ALL elements on the same horizontal row
                                const nearby = Array.from(document.querySelectorAll('*'))
                                    .filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0 && Math.abs((r.top+r.height/2)-pageCenterY) < 50 && b.children.length === 0; })
                                    .map(b => { const r = b.getBoundingClientRect(); const txt=(b.innerText||'').trim().slice(0,25); const cls=(b.className||'').toString().slice(0,40); return `<${b.tagName}>"${txt}"[${cls}]@x${Math.round(r.left)}`; });
                                return {error: 'no_btn_right_of_text', debug_pageY: Math.round(pageCenterY), debug_nearby: nearby.join(' | ')};
                            }

                            // Sort left-to-right: first = ">" (single step), not ">>" (jump to last)
                            btns.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                            const nextBtn = btns[0];
                            const dis = nextBtn.getAttribute('disabled');
                            const aria = nextBtn.getAttribute('aria-disabled');
                            const cls2 = (nextBtn.className || '').toString();
                            if (dis !== null || aria === 'true' ||
                                cls2.includes('disabled') || cls2.includes('inactive')) {
                                return {last: true};
                            }
                            // Scroll the button into the CENTER of the viewport so that
                            // page.mouse.click(x, y) lands within the visible 800px window.
                            // Without this, the pagination bar may be below y=800 even after
                            // the earlier scrollIntoView (ICICI uses a tall inner scroll panel).
                            nextBtn.scrollIntoView({block: 'center', inline: 'nearest'});
                            // getBoundingClientRect after scroll returns viewport-relative coords
                            const r = nextBtn.getBoundingClientRect();
                            return {
                                x: Math.round(r.left + r.width / 2),
                                y: Math.round(r.top + r.height / 2),
                                html: nextBtn.outerHTML.slice(0, 100),
                                debug_pageY: Math.round(pageCenterY),
                                debug_btnY: Math.round(r.top + r.height / 2),
                            };
                        }""")

                    if next_coords is None:
                        print(f"  [next_page] No Next button found — last page ({len(records)} total)")
                        break
                    if next_coords.get("last"):
                        print(f"  [next_page] On last page — done ({len(records)} total)")
                        break
                    if next_coords.get("error"):
                        debug_extra = ""
                        if next_coords.get("debug_pageY"):
                            debug_extra += f" pageY={next_coords['debug_pageY']}"
                        if next_coords.get("debug_nearby"):
                            debug_extra += f" nearby_btns=[{next_coords['debug_nearby']}]"
                        print(f"  [next_page] JS error: {next_coords['error']}{debug_extra} — stopping")
                        break

                    btn_html = next_coords.get("html", "")
                    debug_info = ""
                    if next_coords.get("debug_pageY"):
                        debug_info = f" pageY={next_coords['debug_pageY']} btnY={next_coords['debug_btnY']}"
                    print(f"  [next_page] Clicking Next at ({next_coords['x']:.0f},{next_coords['y']:.0f}) — {len(records)} rows so far… btn={btn_html!r}{debug_info}")
                    self._page.mouse.click(next_coords["x"], next_coords["y"])
                    time.sleep(3)  # Angular re-renders the table — no networkidle on SPA

                    # Stuck-page guard: if no new records were added after the click,
                    # the page didn't advance — stop to avoid an infinite loop.
                    if len(records) == records_before_page and records_before_page > 0:
                        print(f"  [next_page] No new rows after click — pagination stuck, stopping ({len(records)} total)")
                        break

                    continue  # loop back and process the new page's rows

                else:
                    # ── Scroll / "Load More" mode (default) ──────────────────
                    prev_count = len(rows)

                    if view_more_sel:
                        view_more = self._page.query_selector(view_more_sel)
                        if view_more and view_more.is_visible():
                            print(f"  Loading more rows via button (have {len(records)} so far)…")
                            view_more.click()
                            self._page.wait_for_load_state("networkidle", timeout=15000)
                            time.sleep(1)
                            continue

                    # No "View More" button — try auto-scrolling for infinite scroll pages
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
                            val = float(raw.replace("$", "").replace("₹", "").replace(",", ""))
                            if 0 < val < 10_000_000:  # 1 crore limit for INR accounts
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
            // Match $1,234.56 OR amounts without $ like 1,234.56 / +1,234.56 / -1,234.56
            // (BofA shows amounts as "+ 1,234.56" without a dollar sign)
            const dollarRe = /(?:\\$[\\d,]+\\.\\d{2}|(?:^|[\\s(+\\-])\\d{1,3}(?:,\\d{3})*\\.\\d{2})/;
            const dateRe = /\\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\\.?\\s+\\d{1,2}|\\d{1,2}\\/\\d{1,2}/i;

            // Strategy 1: known data-testid patterns and bank-specific selectors
            const knownSels = [
                // Bank of America
                "tr.activity-row",
                "tr[class*='activity-row']",
                "tr[class*='activity']",
                "div[class*='activity-row']",
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
                    // Require at least 1 row for highly specific selectors (e.g. tr.activity-row),
                    // at least 2 for generic class-contains selectors
                    const minCount = (sel.includes("activity-row") || sel.includes("testid")) ? 1 : 2;
                    if (els.length >= minCount) return { selector: sel, count: els.length, sample: els[0]?.outerHTML?.substring(0, 400) || "" };
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
            # If a TOTP secret is stored in credentials, use it automatically
            # (enables background sync for banks configured as otp_interactive)
            if self._creds and self._creds.totp_secret:
                otp_code = generate_totp(self._creds.totp_secret)
            elif self._mfa_callback:
                # Server-side (Smart Connect): block until the API receives the OTP
                otp_code = self._mfa_callback()
            elif self._is_automated:
                # Automated mode with no TOTP and no callback — can't proceed without blocking
                bank_name = self._cfg.get("bank_display_name", self._cfg.get("bank_key", "this bank"))
                raise RuntimeError(
                    f"Automated sync for {bank_name} requires a TOTP secret for OTP automation. "
                    f"Store your TOTP secret in the keychain "
                    f"(python -m bank_fetcher setup --bank {self._cfg['bank_key']}), "
                    "or click Re-login to authenticate manually."
                )
            else:
                # Local dev / interactive mode: prompt in the terminal
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

        # Use config-specified engine/channel if either is set; otherwise auto-detect
        # from the system's default browser so the user sees a familiar window.
        # (Discover needs browser_channel="chrome" to avoid fingerprinting, so we
        # respect browser_channel even when browser_engine is absent.)
        if "browser_engine" in self._cfg or "browser_channel" in self._cfg:
            engine = self._cfg.get("browser_engine", "chromium")
            channel = self._cfg.get("browser_channel", None)
        else:
            engine, channel = _detect_default_browser()
            print(f"  Auto-detected browser: {engine}{f' ({channel})' if channel else ''}")

        ctx_kwargs: dict = dict(
            accept_downloads=False,  # never download files during scraping
            viewport={"width": 1280, "height": 800},
        )
        if storage_state is not None:
            ctx_kwargs["storage_state"] = str(storage_state)

        # Residential proxy — set PROXY_URL env var on Cloud Run to route headless
        # Chromium through a non-datacenter IP so banks don't flag it as a bot.
        # Format: http://user:pass@host:port  (Bright Data / Oxylabs / Smartproxy etc.)
        # When not set (local dev, CI), traffic goes direct — no change in behaviour.
        proxy_url = os.environ.get("PROXY_URL", "").strip()
        if proxy_url and use_headless:
            ctx_kwargs["proxy"] = {"server": proxy_url}
            print(f"  Proxy: residential proxy active (headless)")
        elif use_headless:
            print(f"  Proxy: none (set PROXY_URL env var on Cloud Run to enable)")

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
            # Add sandbox-disable flags when running headless (required inside Docker / Cloud Run)
            # Extra args reduce bot-detection signals from headless Chromium
            extra_args = (
                [
                    "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--window-size=1280,800",
                ]
                if use_headless else []
            )
            try:
                self._browser = self._pw.chromium.launch(
                    headless=use_headless,
                    channel=channel,
                    args=["--disable-extensions"] + extra_args,
                )
            except Exception:
                self._browser = self._pw.chromium.launch(
                    headless=use_headless,
                    args=["--disable-extensions"] + extra_args,
                )

        # Config can override user agent (e.g. Discover needs a current UA to pass browser check).
        # Fall back to a modern Chrome UA for bundled Chromium (channel=None) which otherwise
        # exposes "Chrome for Testing" in its user agent string.
        cfg_ua = self._cfg.get("user_agent")
        if cfg_ua:
            ctx_kwargs["user_agent"] = cfg_ua
        elif engine != "webkit" and channel is None:
            ctx_kwargs["user_agent"] = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )

        self._context = self._browser.new_context(**ctx_kwargs)
        self._page = self._context.new_page()

        # Cancel any download that somehow slips through (belt-and-suspenders on top of
        # accept_downloads=False in the context — Chrome sometimes still queues downloads
        # in headed mode when the page triggers them natively).
        self._page.on("download", lambda dl: dl.cancel())

        # Block explicit file-export requests (PDF / XLS download buttons on ICICI etc.)
        # so that clicking the wrong element never saves a file to disk.
        def _abort_file_exports(route):
            url = route.request.url.lower()
            _DOWNLOAD_SIGNALS = (
                "filetype=pdf", "filetype=xls", "filetype=xlsx", "filetype=csv",
                "downloadstatement", "download_statement", "exportstatement",
                "export_statement", "statementdownload", "/download?", "=download",
                "format=pdf", "format=xls", "exporttype",
            )
            if any(sig in url for sig in _DOWNLOAD_SIGNALS):
                print(f"  [download-block] Aborted export request: {url[:120]}")
                route.abort()
            else:
                route.continue_()

        self._page.route("**/*", _abort_file_exports)

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
    r'|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}'  # DD MMM YYYY
    r'|\d{1,2}/\d{1,2}(?:/\d{2,4})?'
    r'|\d{4}-\d{2}-\d{2}',
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r'[-+]?[$₹][\d,]+\.\d{2}')
_PLAIN_AMOUNT_RE = re.compile(r'[\d,]+\.\d{2}')


def _extract_amount_str(raw: str) -> str:
    """Extract a numeric amount string from raw cell text.
    Handles $12.34, ₹1,23,456.78, or plain 12,345.67 (Indian banks omit symbol in table cells).
    """
    if not raw:
        return ""
    m = _AMOUNT_RE.search(raw)
    if m:
        return m.group(0)
    # Fallback: plain decimal without currency symbol (e.g. ICICI table cells)
    m2 = _PLAIN_AMOUNT_RE.search(raw)
    return m2.group(0) if m2 else ""


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
    """Parse a transaction date string. Returns None for unparseable text (e.g. 'Processing').
    Handles multi-date cells (ICICI has both transaction date and value date) by taking the first.
    """
    # Normalise: collapse all whitespace so "23 Apr 2026\n20 Apr 2026" → "23 Apr 2026 20 Apr 2026"
    text = " ".join(text.split())
    # Try exact-match formats first
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%b %d %Y", "%b %d, %Y",
                "%d %b %Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    # If the full string didn't match, extract the first date token via _DATE_RE
    m_re = _DATE_RE.search(text)
    if m_re:
        candidate = m_re.group(0).strip()
        if candidate != text.strip():
            return _parse_date(candidate)
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
