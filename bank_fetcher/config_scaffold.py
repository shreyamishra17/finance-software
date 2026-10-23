from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

_CONFIG_DIR = Path(__file__).resolve().parent / "configs"


# ------------------------------------------------------------------ #
# Public entry point
# ------------------------------------------------------------------ #

def scaffold_bank_config(bank_key: str, login_url: str) -> Path:
    """
    Full scaffold flow:
      1. Run playwright codegen so user records the login + statements flow
      2. Parse generated code into step dicts
      3. Tag credential fills ($username / $password)
      4. Split into login_steps / statements_steps
      5. Collect MFA config
      6. Collect download selectors
      7. Write configs/<bank_key>_fetcher_config.json
    Returns path to the saved config file.
    """
    bank_display_name = input(f"Display name for this bank (e.g. 'Chase'): ").strip()
    if not bank_display_name:
        bank_display_name = bank_key.replace("_", " ").title()

    print(f"\n[{bank_key}] Opening browser recorder. Please:")
    print("  1. Log into your account completely (including MFA if prompted)")
    print("  2. Navigate to the statements / account documents page")
    print("  3. Click on one PDF download link to record how it's triggered")
    print("  4. Close the browser window when done.\n")

    source = _run_codegen(login_url)
    if not source.strip():
        raise RuntimeError(
            "No actions were recorded (empty output). "
            "Make sure to perform the full login + navigation flow before closing."
        )

    steps = _parse_codegen_output(source)
    if not steps:
        raise RuntimeError(
            "Could not parse any steps from the recorded session. "
            "Check that the browser was opened at the correct login URL."
        )

    steps = _tag_credential_steps(steps)
    login_steps, statements_steps = _split_login_statements(steps)
    mfa_mode, mfa_selectors = _collect_mfa_config(bank_display_name)

    # Insert maybe_mfa step at the right position if MFA was present
    if mfa_mode != "none":
        mfa_probe_selector = mfa_selectors.get("otp_field", "")
        maybe_mfa_step = {
            "action": "maybe_mfa",
            "probe_selector": mfa_probe_selector,
            "probe_timeout": 5000,
        }
        # Insert just before the final wait_for_url in login_steps
        insert_at = next(
            (i for i, s in enumerate(login_steps) if s["action"] == "wait_for_url"),
            len(login_steps),
        )
        login_steps.insert(insert_at, maybe_mfa_step)

    download_cfg = _collect_download_config()
    download_dir_hint = input(
        "\nLast 4 digits of the account number (used for filenames, e.g. '7238'): "
    ).strip()

    cfg = _build_config(
        bank_key=bank_key,
        bank_display_name=bank_display_name,
        mfa_mode=mfa_mode,
        mfa_selectors=mfa_selectors,
        download_dir_hint=download_dir_hint,
        login_steps=login_steps,
        statements_steps=statements_steps,
        download_config=download_cfg,
    )

    out_path = _CONFIG_DIR / f"{bank_key}_fetcher_config.json"
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    print(f"\nSaved: {out_path}")
    print(f"\nNext steps:")
    print(f"  python -m bank_fetcher setup --bank {bank_key}")
    print(f"  python -m bank_fetcher --bank {bank_key} --months 3")

    return out_path


# ------------------------------------------------------------------ #
# Codegen runner
# ------------------------------------------------------------------ #

def _run_codegen(login_url: str) -> str:
    """
    Run `playwright codegen <url> --output <tmpfile>` and return the generated source.
    Cleans up the temp file on exit.
    """
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
        tmpfile = Path(f.name)

    try:
        subprocess.run(
            ["playwright", "codegen", login_url, "--output", str(tmpfile)],
            check=False,  # user may close browser normally (non-zero exit is fine)
        )
        if tmpfile.exists():
            return tmpfile.read_text(encoding="utf-8")
        return ""
    finally:
        tmpfile.unlink(missing_ok=True)


# ------------------------------------------------------------------ #
# Codegen output parser
# ------------------------------------------------------------------ #

def _parse_codegen_output(source: str) -> list[dict]:
    """
    Parse Playwright codegen Python output into our step dicts.
    Handles both direct page.* calls and chained get_by_*().action() calls.
    Fill values retain their recorded value (credential tagging done later).
    """
    steps: list[dict] = []

    for line in source.splitlines():
        line = line.strip()

        # goto
        m = re.search(r'page\.goto\("([^"]+)"', line)
        if m:
            url = m.group(1)
            if url.startswith("https://"):
                steps.append({"action": "goto", "url": url})
            continue

        # wait_for_url
        m = re.search(r'page\.wait_for_url\("([^"]+)"', line)
        if m:
            steps.append({"action": "wait_for_url", "pattern": m.group(1)})
            continue

        # wait_for_load_state
        m = re.search(r'page\.wait_for_load_state\("([^"]+)"', line)
        if m:
            steps.append({"action": "wait_for_load_state", "state": m.group(1)})
            continue

        # wait_for_selector (direct)
        m = re.search(r'page\.wait_for_selector\("([^"]+)"', line)
        if m:
            steps.append({"action": "wait_for_selector", "selector": m.group(1)})
            continue

        # page.fill("selector", "value")
        m = re.search(r'page\.fill\("([^"]+)",\s*"([^"]*)"', line)
        if m:
            steps.append({"action": "fill", "selector": m.group(1), "value": m.group(2)})
            continue

        # page.click("selector")
        m = re.search(r'page\.click\("([^"]+)"', line)
        if m:
            steps.append({"action": "click", "selector": m.group(1)})
            continue

        # Chained: page.get_by_*(…).fill("value")  or  .fill('value')
        fill_chain = re.search(
            r'(page\.(?:get_by_\w+)\([^)]*\)(?:\.(?:get_by_\w+)\([^)]*\))*)'
            r'\.fill\(["\']([^"\']*)["\']',
            line,
        )
        if fill_chain:
            selector = _convert_locator_to_selector(fill_chain.group(1))
            steps.append({"action": "fill", "selector": selector, "value": fill_chain.group(2)})
            continue

        # Chained: page.get_by_*(…).click()
        click_chain = re.search(
            r'(page\.(?:get_by_\w+)\([^)]*\)(?:\.(?:get_by_\w+)\([^)]*\))*)\.click\(\)',
            line,
        )
        if click_chain:
            selector = _convert_locator_to_selector(click_chain.group(1))
            steps.append({"action": "click", "selector": selector})
            continue

    return steps


def _convert_locator_to_selector(locator_expr: str) -> str:
    """
    Convert Playwright locator chain to a string selector Playwright accepts.

    Examples:
      page.get_by_label("Online ID")          → "label=Online ID"
      page.get_by_role("button", name="Sign") → "role=button[name='Sign']"
      page.get_by_placeholder("Password")     → "placeholder=Password"
      page.get_by_test_id("login-btn")        → "data-testid=login-btn"
    """
    # get_by_label("X")
    m = re.search(r'get_by_label\(["\']([^"\']+)["\']', locator_expr)
    if m:
        return f"label={m.group(1)}"

    # get_by_placeholder("X")
    m = re.search(r'get_by_placeholder\(["\']([^"\']+)["\']', locator_expr)
    if m:
        return f"placeholder={m.group(1)}"

    # get_by_role("role", name="X")
    m = re.search(r'get_by_role\(["\']([^"\']+)["\'].*?name=["\']([^"\']+)["\']', locator_expr)
    if m:
        return f"role={m.group(1)}[name='{m.group(2)}']"

    # get_by_role("role") without name
    m = re.search(r'get_by_role\(["\']([^"\']+)["\']', locator_expr)
    if m:
        return f"role={m.group(1)}"

    # get_by_test_id("X")
    m = re.search(r'get_by_test_id\(["\']([^"\']+)["\']', locator_expr)
    if m:
        return f"data-testid={m.group(1)}"

    # get_by_text("X")
    m = re.search(r'get_by_text\(["\']([^"\']+)["\']', locator_expr)
    if m:
        return f"text={m.group(1)}"

    # Fallback: return the raw expression
    return locator_expr


# ------------------------------------------------------------------ #
# Interactive prompts
# ------------------------------------------------------------------ #

def _tag_credential_steps(steps: list[dict]) -> list[dict]:
    """Ask user which fill steps are username and password."""
    fill_steps = [(i, s) for i, s in enumerate(steps) if s["action"] == "fill"]

    if not fill_steps:
        print("No fill steps detected — credentials cannot be tagged automatically.")
        return steps

    print(f"\nRecorded {len(steps)} actions. Fill steps detected:")
    for idx, (i, s) in enumerate(fill_steps):
        display_val = s["value"][:3] + "•••" if len(s["value"]) > 3 else "•••"
        print(f"  [{idx}] fill {s['selector']} = \"{display_val}\"")

    # Username
    default_user_idx = 0
    raw = input(f"\nWhich fill index is your USERNAME? [{default_user_idx}]: ").strip()
    user_fill_idx = int(raw) if raw.isdigit() else default_user_idx
    if 0 <= user_fill_idx < len(fill_steps):
        step_pos = fill_steps[user_fill_idx][0]
        steps[step_pos]["value"] = "$username"

    # Password
    default_pass_idx = min(1, len(fill_steps) - 1)
    raw = input(f"Which fill index is your PASSWORD? [{default_pass_idx}]: ").strip()
    pass_fill_idx = int(raw) if raw.isdigit() else default_pass_idx
    if 0 <= pass_fill_idx < len(fill_steps):
        step_pos = fill_steps[pass_fill_idx][0]
        steps[step_pos]["value"] = "$password"

    return steps


def _split_login_statements(steps: list[dict]) -> tuple[list[dict], list[dict]]:
    """Ask user where statements navigation begins. Split accordingly."""
    print("\nFull step list:")
    for i, s in enumerate(steps):
        action = s["action"]
        detail = s.get("url") or s.get("selector") or s.get("pattern") or s.get("state") or ""
        value = f' = {s["value"]}' if action == "fill" else ""
        print(f"  {i:2d}: {action}  {detail}{value}")

    total = len(steps)
    raw = input(
        f"\nAt which step index does STATEMENTS NAVIGATION begin? "
        f"(steps 0..N-1 = login, steps N..end = navigation) [{total}]: "
    ).strip()

    split_at = int(raw) if raw.isdigit() else total
    split_at = max(0, min(split_at, total))

    login_steps = steps[:split_at]
    statements_steps = steps[split_at:]
    return login_steps, statements_steps


def _collect_mfa_config(bank_display_name: str) -> tuple[str, dict]:
    """Ask about MFA. Returns (mfa_mode, mfa_selectors dict)."""
    has_mfa = input("\nWas there an MFA/OTP prompt during login? [y/N]: ").strip().lower()
    if has_mfa != "y":
        return "none", {}

    print(f"\nIn the browser DevTools (F12 → Inspector), find the OTP input field selector.")
    otp_field = input("  OTP input field CSS selector (e.g. '#otp-input'): ").strip()
    submit_btn = input("  OTP submit button CSS selector (e.g. '#verify-btn'): ").strip()

    use_totp = input("\nDo you use a TOTP authenticator app (vs. SMS/email)? [y/N]: ").strip().lower()
    mfa_mode = "totp" if use_totp == "y" else "otp_interactive"

    return mfa_mode, {"otp_field": otp_field, "submit_button": submit_btn}


def _collect_download_config() -> dict:
    """Ask for statement row and PDF link selectors."""
    print("\n--- Download selectors ---")
    print("Open the statements page in your browser with DevTools (F12 → Inspector).")
    print("Find the CSS selector for:")
    print("  (a) Each STATEMENT ROW  (the repeating li / tr / div that contains a PDF link)")
    print("  (b) The PDF DOWNLOAD LINK inside each row\n")

    row_selector = input("Statement row selector (e.g. 'tr.statement-row'): ").strip()
    link_selector = input("PDF link selector (e.g. 'a[href$=\".pdf\"]'): ").strip()

    return {"row_selector": row_selector, "link_selector": link_selector}


# ------------------------------------------------------------------ #
# Config assembler
# ------------------------------------------------------------------ #

def _build_config(
    bank_key: str,
    bank_display_name: str,
    mfa_mode: str,
    mfa_selectors: dict,
    download_dir_hint: str,
    login_steps: list[dict],
    statements_steps: list[dict],
    download_config: dict,
) -> dict:
    cfg: dict = {
        "bank_key": bank_key,
        "bank_display_name": bank_display_name,
        "keyring_service": "fifo_bank_fetcher",
        "keyring_username_key": f"{bank_key}_username",
        "keyring_password_key": f"{bank_key}_password",
        "mfa_mode": mfa_mode,
        "download_dir_hint": download_dir_hint,
        "headless": False,
        "timeouts_ms": {
            "page_load": 30000,
            "element_wait": 10000,
            "download_wait": 60000,
        },
        "max_statements": 12,
        "navigation": {
            "logout_url": "",  # user can fill in manually
        },
        "login_steps": login_steps,
        "statements_steps": statements_steps,
        "download_config": download_config,
    }

    if mfa_mode == "totp":
        cfg["keyring_totp_secret_key"] = f"{bank_key}_totp_secret"

    if mfa_selectors:
        cfg["mfa_selectors"] = mfa_selectors

    return cfg
