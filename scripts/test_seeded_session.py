"""
FEASIBILITY TEST: does Bank of America's *device trust* survive being carried
out of your real Chrome into a fresh browser?

This is the experiment behind "sync while the PC is off." The plan is:
  real Chrome (trusted, holds BoA's long-lived device cookie)
      → export those cookies
      → seed them into a brand-new browser (stand-in for a cloud worker)
      → log in with the saved password
      → if NO OTP appears, device trust carried, and a cloud worker could do
        the same thing 24/7.

What this script does, end to end:
  1. Reads BoA cookies straight from your real Chrome's on-disk cookie store
     and decrypts them (the device-trust cookie is HttpOnly, so only the
     on-disk store / extension cookies API can see it — document.cookie can't).
  2. Launches a FRESH Playwright Chromium (a different browser instance — the
     stand-in for "the cloud"), seeds those cookies, and navigates to BoA.
  3. Reports one of three verdicts:
        FULL SESSION CARRIED  — cookies alone logged us in, no password needed
        LOGGED IN, NO OTP     — saved-password login skipped OTP  ✅ trust carried
        OTP REQUIRED          — trust did NOT carry                ❌

Run:
    FIFO_UID=6fe3ddc783bf485399652c36c17430a9 python scripts/test_seeded_session.py

Env knobs:
    FIFO_UID        which user's saved BoA password to use for the login step
    CHROME_PROFILE  Chrome profile dir name (default: "Default")
    SEED_HEADLESS   "1" to run the seeded browser headless (cloud-like). Default
                    is headful so you can watch whether an OTP screen appears.
    SKIP_LOGIN      "1" to only test whether cookies alone carry a live session
                    (no password submitted).

Artifacts: data/debug/seeded_*.png / .txt
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

DEBUG_DIR = ROOT / "data" / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

CHROME_PROFILE = os.environ.get("CHROME_PROFILE", "Default")
SEED_HEADLESS = os.environ.get("SEED_HEADLESS", "0") == "1"
SKIP_LOGIN = os.environ.get("SKIP_LOGIN", "0") == "1"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ----------------------------------------------------------------------------- #
# 1. Read + decrypt BoA cookies from the real Chrome on-disk store (macOS)
# ----------------------------------------------------------------------------- #

def _chrome_cookie_db() -> Path:
    """Locate the Chrome Cookies SQLite file for the chosen profile."""
    base = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / CHROME_PROFILE
    candidates = [base / "Network" / "Cookies", base / "Cookies"]  # modern, then legacy
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        f"Could not find a Chrome Cookies DB for profile {CHROME_PROFILE!r}. "
        f"Looked at: {[str(c) for c in candidates]}. "
        f"Set CHROME_PROFILE to the right profile dir name."
    )


def _chrome_safe_storage_key() -> bytes:
    """Derive the AES key Chrome uses to encrypt cookie values on macOS."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives.hashes import SHA1
    # The raw secret lives in the login keychain under "Chrome Safe Storage".
    # This call may trigger a one-time keychain permission prompt — click Allow.
    raw = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage", "-a", "Chrome"]
    ).strip()
    kdf = PBKDF2HMAC(algorithm=SHA1(), length=16, salt=b"saltysalt", iterations=1003)
    return kdf.derive(raw)


def _decrypt_value(encrypted: bytes, key: bytes) -> str:
    """Decrypt a Chrome 'v10' cookie value (AES-128-CBC, IV = 16 spaces)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if not encrypted or encrypted[:3] != b"v10":
        # Unencrypted (rare) — return as-is.
        try:
            return encrypted.decode("utf-8")
        except Exception:
            return ""
    enc = encrypted[3:]
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
    dec = cipher.decryptor()
    data = dec.update(enc) + dec.finalize()
    # Strip PKCS7 padding.
    if data:
        pad = data[-1]
        if 0 < pad <= 16:
            data = data[:-pad]
    # Newer Chrome prepends a 32-byte SHA256 domain hash to the plaintext.
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data[32:].decode("utf-8", errors="replace")


def _chrome_us_to_unix(expires_utc: int) -> float:
    """Chrome stores expiry as microseconds since 1601-01-01. Convert to unix secs."""
    if not expires_utc:
        return -1  # session cookie
    return expires_utc / 1_000_000 - 11644473600


def export_boa_cookies() -> list[dict]:
    """Pull every bankofamerica.com cookie out of the real Chrome and decrypt it."""
    db_path = _chrome_cookie_db()
    key = _chrome_safe_storage_key()

    # Chrome holds a lock on the live DB; work on a copy.
    tmp = Path(tempfile.mkdtemp()) / "Cookies"
    shutil.copy2(db_path, tmp)

    conn = sqlite3.connect(str(tmp))
    rows = conn.execute(
        "SELECT host_key, name, value, encrypted_value, path, is_secure, "
        "is_httponly, expires_utc FROM cookies WHERE host_key LIKE '%bankofamerica.com%'"
    ).fetchall()
    conn.close()

    cookies: list[dict] = []
    for host, name, value, enc, path, is_secure, is_httponly, expires_utc in rows:
        val = value or _decrypt_value(enc, key)
        if not val:
            continue
        exp = _chrome_us_to_unix(expires_utc)
        ck = {
            "name": name,
            "value": val,
            "domain": host,
            "path": path or "/",
            "secure": bool(is_secure),
            "httpOnly": bool(is_httponly),
            "sameSite": "Lax",
        }
        if exp > 0:
            ck["expires"] = exp
        cookies.append(ck)

    # Save a copy for inspection (values redacted to last 4 chars).
    redacted = [
        {**c, "value": ("…" + c["value"][-4:]) if len(c["value"]) > 4 else "…"}
        for c in cookies
    ]
    (DEBUG_DIR / "seeded_cookies.json").write_text(json.dumps(redacted, indent=2))
    return cookies


# ----------------------------------------------------------------------------- #
# 2. Seed a fresh browser + run the test
# ----------------------------------------------------------------------------- #

def _dump(page, tag: str) -> None:
    url = title = ""
    try:
        url = page.url
        title = page.title()
    except Exception:
        pass
    print(f"  [{tag}] url={url}")
    print(f"  [{tag}] title={title!r}")
    try:
        page.screenshot(path=str(DEBUG_DIR / f"seeded_{tag}.png"), full_page=True)
    except Exception:
        pass
    try:
        body = page.inner_text("body")[:4000]
        (DEBUG_DIR / f"seeded_{tag}.txt").write_text(
            f"URL: {url}\nTITLE: {title}\n\n{body}", encoding="utf-8"
        )
    except Exception:
        pass


def _looks_logged_in(page) -> bool:
    from urllib.parse import urlparse
    try:
        u = urlparse(page.url or "")
        host = (u.netloc or "").lower()
        path = (u.path or "").lower()
        query = (u.query or "").lower()
        # SiteMinder bounce back to login carries SMAUTHReason — definitely NOT logged in,
        # even though the TARGET= param echoes ".../myaccounts/".
        if "smauthreason" in query:
            return False
        # Real logged-in pages live on secure.bankofamerica.com with /myaccounts in the PATH.
        if host.endswith("secure.bankofamerica.com") and "myaccounts" in path:
            return True
    except Exception:
        pass
    # A visible login form means we are NOT logged in.
    try:
        if page.locator("#oid").count() > 0:
            return False
    except Exception:
        pass
    for sel in ("a[href*='estatements']", "a[href*='signoff']",
                "a[href*='sign-out']", "[class*='AccountSummary']"):
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            pass
    return False


def _otp_present(page) -> bool:
    try:
        if any(s in (page.url or "").lower() for s in ("/authcode", "/auth/signon")):
            # /auth/signon alone can be the plain login page; require a code field too
            pass
    except Exception:
        pass
    for sel in ("#tlpvt-otp-input", "input[autocomplete='one-time-code']",
                "[id*='otp']", "[id*='Otp']", "[id*='passcode']", "[name*='code']",
                "[id*='authcode']", "[id*='challenge']"):
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            pass
    try:
        if "/authcode" in (page.url or "").lower():
            return True
    except Exception:
        pass
    return False


def main() -> None:
    print("=" * 70)
    print("BoA device-trust portability test")
    print("=" * 70)

    # --- export cookies from the real Chrome ---
    print(f"\n[1/3] Exporting BoA cookies from real Chrome (profile={CHROME_PROFILE!r})…")
    try:
        cookies = export_boa_cookies()
    except Exception as e:
        print(f"  ERROR exporting cookies: {e}")
        print("  (If this is a keychain prompt, click Allow and re-run.)")
        sys.exit(2)
    if not cookies:
        print("  No bankofamerica.com cookies found in Chrome. Log into BoA in your")
        print("  real Chrome first (so the device-trust cookie exists), then re-run.")
        sys.exit(2)
    httponly = sum(1 for c in cookies if c["httpOnly"])
    print(f"  Exported {len(cookies)} cookies ({httponly} HttpOnly — these are the")
    print(f"  ones document.cookie can't see; the device-trust cookie is among them).")

    # --- credentials for the login step ---
    creds = None
    if not SKIP_LOGIN:
        from db import set_current_uid
        from bank_fetcher import _load_fetcher_config
        from bank_fetcher.credential_manager import get_credentials
        uid = os.environ.get("FIFO_UID", "").strip()
        if uid:
            set_current_uid(uid)
        try:
            creds = get_credentials(_load_fetcher_config("boa"))
            print(f"  Loaded saved BoA password for user={creds.username[:3]}*** "
                  f"(used only if cookies don't already carry a live session).")
        except Exception as e:
            print(f"  WARNING: no saved password ({e}). Will test cookies-only.")
            creds = None

    # --- seed a fresh browser ---
    from playwright.sync_api import sync_playwright
    print(f"\n[2/3] Launching a FRESH browser (headless={SEED_HEADLESS}) — the cloud stand-in…")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=SEED_HEADLESS,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled", "--disable-infobars",
                "--window-size=1280,800",
            ],
        )
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, user_agent=USER_AGENT)
        ctx.add_cookies(cookies)
        print(f"  Seeded {len(cookies)} cookies into the fresh context.")
        page = ctx.new_page()

        print("  Navigating to bankofamerica.com with the seeded cookies…")
        page.goto("https://www.bankofamerica.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        _dump(page, "after_seed")

        verdict = None

        # CASE A: cookies alone carried a live session.
        if _looks_logged_in(page):
            verdict = "FULL_SESSION"
        else:
            # Try the secure landing directly in case the homepage didn't redirect.
            try:
                page.goto("https://secure.bankofamerica.com/myaccounts/",
                          wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
            except Exception:
                pass
            _dump(page, "after_secure")
            if _looks_logged_in(page):
                verdict = "FULL_SESSION"

        # CASE B: not logged in → do the saved-password login and watch for OTP.
        if verdict is None and creds and not SKIP_LOGIN:
            print("\n  Cookies didn't carry a live session — testing saved-password login")
            print("  on the seeded (device-trusted?) browser. Watching for OTP…")
            try:
                page.goto("https://www.bankofamerica.com/", wait_until="domcontentloaded")
                page.wait_for_selector("#oid", timeout=30000)
                page.fill("#oid", creds.username)
                page.fill("#pass", creds.password)
                page.click("#secure-signin-submit")
                page.wait_for_timeout(12000)
            except Exception as e:
                print(f"  login step error: {e}")
            _dump(page, "post_login")

            if _otp_present(page):
                verdict = "OTP_REQUIRED"
            elif _looks_logged_in(page):
                verdict = "LOGIN_NO_OTP"
            else:
                verdict = "UNKNOWN"
        elif verdict is None:
            verdict = "COOKIES_ONLY_FAILED"

        browser.close()

    # --- report ---
    print("\n" + "=" * 70)
    print("[3/3] VERDICT")
    print("=" * 70)
    if verdict == "FULL_SESSION":
        print("  ✅ FULL SESSION CARRIED — the seeded cookies logged us straight in.")
        print("     A cloud worker could scrape with zero login while the session lasts.")
    elif verdict == "LOGIN_NO_OTP":
        print("  ✅ LOGGED IN, NO OTP — saved-password login on the fresh browser")
        print("     skipped OTP. DEVICE TRUST CARRIED. The cloud-worker plan is viable.")
    elif verdict == "OTP_REQUIRED":
        print("  ❌ OTP REQUIRED — the fresh browser was challenged despite the seeded")
        print("     cookies. Device trust did NOT carry on cookies alone. Next hedge:")
        print("     match user-agent (done) + try a fixed residential egress IP.")
    elif verdict == "COOKIES_ONLY_FAILED":
        print("  ⚠️  Cookies alone didn't carry a session, and login was skipped")
        print("     (SKIP_LOGIN=1 or no saved password). Re-run with a saved BoA")
        print("     password and SKIP_LOGIN unset to test the device-trust login path.")
    else:
        print(f"  ⚠️  UNCLEAR ({verdict}) — inspect data/debug/seeded_*.png/.txt to see")
        print("     where the browser landed.")
    print(f"\n  Artifacts: {DEBUG_DIR}/seeded_*.png / .txt")


if __name__ == "__main__":
    main()
