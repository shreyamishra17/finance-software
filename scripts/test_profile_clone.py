"""
DECISIVE TEST: is Bank of America's device trust portable when we carry the
WHOLE browser profile (cookies + localStorage + IndexedDB), not just cookies?

Background: seeding cookies alone into a fresh browser still triggered OTP, even
from the same machine/IP/user-agent. That ruled out cookies + IP + UA. The
remaining suspect is browser storage (localStorage / IndexedDB), where modern
banks often stash a "remembered device" token. This test clones the entire
Chrome profile and launches real Google Chrome against the COPY.

Verdict:
  FULL SESSION CARRIED  — the clone was already logged in (no password needed)
  LOGGED IN, NO OTP     — saved-password login on the clone skipped OTP  ✅
                          → device trust IS portable; cloud-worker plan is alive
  OTP REQUIRED          — even a full profile clone got challenged           ❌
                          → trust is machine-bound; cloud-while-off is dead

IMPORTANT: quit Google Chrome fully before running, so the profile copy is
consistent (Chrome holds locks on its leveldb stores while open). We copy the
profile to a temp dir and never touch your real one.

Run:
    FIFO_UID=6fe3ddc783bf485399652c36c17430a9 python scripts/test_profile_clone.py

Env knobs:
    FIFO_UID        which user's saved BoA password to use for the login step
    CHROME_PROFILE  Chrome profile dir name to clone (default: "Default")
    CLONE_HEADLESS  "1" to run the cloned browser headless (cloud-like). Default
                    headful so you can watch for an OTP screen.

Artifacts: data/debug/clone_*.png / .txt
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

# Reuse the (pure-logic) detection helpers from the cookie test.
from test_seeded_session import _looks_logged_in, _otp_present  # noqa: E402

DEBUG_DIR = ROOT / "data" / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

CHROME_PROFILE = os.environ.get("CHROME_PROFILE", "Default")
CLONE_HEADLESS = os.environ.get("CLONE_HEADLESS", "0") == "1"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Big/locked/cache dirs we don't need — skipping them keeps the copy fast and
# avoids copying volatile cache state. The trust token (if any) lives in
# Cookies / Local Storage / IndexedDB / Session Storage, which we DO copy.
SKIP_NAMES = {
    "Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache",
    "DawnWebGPUCache", "GrShaderCache", "ShaderCache", "Service Worker",
    "Application Cache", "blob_storage", "Cache_Data", "component_crx_cache",
    "extensions_crx_cache", "OnDeviceHeadSuggestModel", "optimization_guide_model_store",
}


def _chrome_user_data_root() -> Path:
    root = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    if not root.exists():
        raise FileNotFoundError(f"Chrome user-data root not found at {root}")
    return root


def clone_profile() -> Path:
    """Copy the chosen Chrome profile into a temp user-data dir as 'Default'."""
    root = _chrome_user_data_root()
    src_profile = root / CHROME_PROFILE
    if not src_profile.exists():
        raise FileNotFoundError(
            f"Profile {CHROME_PROFILE!r} not found at {src_profile}. "
            f"Set CHROME_PROFILE to the right profile dir name."
        )

    tmp_root = Path(tempfile.mkdtemp(prefix="fifo_clone_"))
    dst_profile = tmp_root / "Default"

    print(f"  Cloning {src_profile} → {dst_profile} (skipping caches)…")
    shutil.copytree(
        src_profile, dst_profile,
        ignore=shutil.ignore_patterns(*SKIP_NAMES),
        symlinks=True, ignore_dangling_symlinks=True,
    )

    # Local State (at the user-data root) holds profile metadata; copy if present.
    local_state = root / "Local State"
    if local_state.exists():
        try:
            shutil.copy2(local_state, tmp_root / "Local State")
        except Exception:
            pass

    return tmp_root


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
        page.screenshot(path=str(DEBUG_DIR / f"clone_{tag}.png"), full_page=True)
    except Exception:
        pass
    try:
        body = page.inner_text("body")[:4000]
        (DEBUG_DIR / f"clone_{tag}.txt").write_text(
            f"URL: {url}\nTITLE: {title}\n\n{body}", encoding="utf-8"
        )
    except Exception:
        pass


def _chrome_running() -> bool:
    try:
        import subprocess
        out = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True)
        return out.returncode == 0
    except Exception:
        return False


def main() -> None:
    print("=" * 70)
    print("BoA device-trust portability test — FULL PROFILE CLONE")
    print("=" * 70)

    if _chrome_running():
        print("\n  ⚠️  Google Chrome appears to be RUNNING. For a consistent clone,")
        print("     fully quit Chrome (Cmd+Q) and re-run. Continuing anyway, but the")
        print("     copied leveldb stores may be partial.\n")

    # --- clone the profile ---
    print(f"\n[1/3] Cloning Chrome profile (profile={CHROME_PROFILE!r})…")
    try:
        user_data_dir = clone_profile()
    except Exception as e:
        print(f"  ERROR cloning profile: {e}")
        sys.exit(2)
    print(f"  Clone ready at {user_data_dir}")

    # --- credentials for the login step ---
    from db import set_current_uid
    from bank_fetcher import _load_fetcher_config
    from bank_fetcher.credential_manager import get_credentials
    uid = os.environ.get("FIFO_UID", "").strip()
    if uid:
        set_current_uid(uid)
    try:
        creds = get_credentials(_load_fetcher_config("boa"))
        print(f"  Loaded saved BoA password for user={creds.username[:3]}*** "
              f"(used only if the clone isn't already logged in).")
    except Exception as e:
        print(f"  WARNING: no saved password ({e}). Will test clone-only.")
        creds = None

    # --- launch REAL Chrome against the clone ---
    from playwright.sync_api import sync_playwright
    print(f"\n[2/3] Launching real Google Chrome on the CLONE (headless={CLONE_HEADLESS})…")
    verdict = None
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(user_data_dir),
            channel="chrome",  # real Google Chrome, so keychain cookie key matches
            headless=CLONE_HEADLESS,
            viewport={"width": 1280, "height": 800},
            user_agent=USER_AGENT,
            args=[
                "--no-first-run", "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars", "--window-size=1280,800",
            ],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print("  Navigating to secure.bankofamerica.com/myaccounts with the cloned profile…")
        try:
            page.goto("https://secure.bankofamerica.com/myaccounts/",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
        except Exception as e:
            print(f"  navigation note: {e}")
        _dump(page, "after_clone")

        if _looks_logged_in(page):
            verdict = "FULL_SESSION"
        else:
            # Not logged in via clone alone → do the saved-password login, watch for OTP.
            if creds:
                print("\n  Clone wasn't already logged in — testing saved-password login")
                print("  on the cloned (device-trusted?) Chrome. Watching for OTP…")
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
            else:
                verdict = "CLONE_ONLY_FAILED"

        ctx.close()

    # --- cleanup the clone (it contains your session data) ---
    try:
        shutil.rmtree(user_data_dir, ignore_errors=True)
        print(f"\n  Cleaned up clone at {user_data_dir}")
    except Exception:
        pass

    # --- report ---
    print("\n" + "=" * 70)
    print("[3/3] VERDICT")
    print("=" * 70)
    if verdict == "FULL_SESSION":
        print("  ✅ FULL SESSION CARRIED — the cloned profile was already logged in.")
        print("     Device trust + session are fully portable. Cloud-worker plan is viable.")
    elif verdict == "LOGIN_NO_OTP":
        print("  ✅ LOGGED IN, NO OTP — saved-password login on the cloned profile")
        print("     skipped OTP. DEVICE TRUST IS PORTABLE (it lives in browser storage,")
        print("     not just cookies). The cloud-worker plan is ALIVE.")
    elif verdict == "OTP_REQUIRED":
        print("  ❌ OTP REQUIRED — even a full profile clone got challenged. Device trust")
        print("     is bound to the physical machine, not portable browser state.")
        print("     CONCLUSION: cloud-sync-while-PC-off is not feasible. Desktop sync")
        print("     (extension, PC on) is the honest ceiling.")
    elif verdict == "CLONE_ONLY_FAILED":
        print("  ⚠️  Clone wasn't logged in and no saved password was available to test")
        print("     the login path. Re-run with a saved BoA password.")
    else:
        print(f"  ⚠️  UNCLEAR ({verdict}) — inspect data/debug/clone_*.png/.txt.")
    print(f"\n  Artifacts: {DEBUG_DIR}/clone_*.png / .txt")


if __name__ == "__main__":
    main()
