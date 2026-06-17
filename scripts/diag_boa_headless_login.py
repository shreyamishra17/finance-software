"""
Diagnostic: perform the BoA headless login exactly like the server's Sync Now
credential path, but capture what page the headless browser actually lands on
after submitting the login form. This tells us whether the "timeout waiting for
**/myaccounts**" is really a verification/OTP challenge (genuine re-login),
a bot block, or a wrong post-login URL pattern.

Run:  FIFO_UID=6fe3ddc783bf485399652c36c17430a9 python scripts/diag_boa_headless_login.py
Artifacts saved to data/debug/boa_login_*.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from db import set_current_uid
from bank_fetcher import _load_fetcher_config
from bank_fetcher.credential_manager import get_credentials
from playwright.sync_api import sync_playwright

DEBUG_DIR = ROOT / "data" / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def main() -> None:
    uid = os.environ.get("FIFO_UID", "").strip()
    if uid:
        set_current_uid(uid)
        print(f"[diag] uid set to {uid}")
    else:
        print("[diag] WARNING: no FIFO_UID set — credential lookup may miss the uid-prefixed key")

    cfg = _load_fetcher_config("boa")
    creds = get_credentials(cfg)
    print(f"[diag] got credentials for user={creds.username[:3]}***")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled", "--disable-infobars",
                "--window-size=1280,800", "--disable-extensions",
            ],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        print("[diag] goto bankofamerica.com")
        page.goto("https://www.bankofamerica.com/", wait_until="domcontentloaded")
        try:
            page.wait_for_selector("#oid", timeout=30000)
        except Exception as e:
            _dump(page, "no_oid")
            print(f"[diag] #oid never appeared: {e}")
            browser.close()
            return

        page.fill("#oid", creds.username)
        page.fill("#pass", creds.password)
        print("[diag] submitting login…")
        page.click("#secure-signin-submit")

        # Give the post-login navigation/challenge time to render, but DON'T
        # demand myaccounts — we want to see whatever shows up.
        page.wait_for_timeout(12000)
        _dump(page, "post_login")

        # Probe a bunch of candidate OTP/verification selectors to see which (if any) match.
        candidates = [
            "#tlpvt-otp-input", "input[name='otp']", "input[autocomplete='one-time-code']",
            "#yodlee-otp", "input[name='code']", "#verify-code",
            "[id*='otp']", "[id*='Otp']", "[id*='passcode']", "[id*='challenge']",
            "[name*='otp']", "[name*='code']",
        ]
        print("[diag] OTP/challenge selector probe:")
        for sel in candidates:
            try:
                n = page.locator(sel).count()
            except Exception:
                n = "err"
            if n:
                print(f"    MATCH {sel} -> {n}")

        browser.close()
    print(f"[diag] done. Artifacts in {DEBUG_DIR}")


def _dump(page, tag: str) -> None:
    url = page.url
    title = ""
    try:
        title = page.title()
    except Exception:
        pass
    print(f"[diag] [{tag}] url={url}")
    print(f"[diag] [{tag}] title={title!r}")
    try:
        page.screenshot(path=str(DEBUG_DIR / f"boa_login_{tag}.png"), full_page=True)
    except Exception as e:
        print(f"[diag] screenshot failed: {e}")
    try:
        body_text = page.inner_text("body")[:4000]
        (DEBUG_DIR / f"boa_login_{tag}.txt").write_text(
            f"URL: {url}\nTITLE: {title}\n\n{body_text}", encoding="utf-8"
        )
        # Print the first 600 chars so we see it inline.
        print(f"[diag] [{tag}] body text (first 600):\n{body_text[:600]}")
    except Exception as e:
        print(f"[diag] body text failed: {e}")


if __name__ == "__main__":
    main()
