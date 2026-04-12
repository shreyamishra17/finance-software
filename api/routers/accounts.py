"""
api/routers/accounts.py — Account management endpoints.

GET    /api/accounts                    list all accounts
POST   /api/accounts                    connect bank (scraper) or upload PDF
GET    /api/accounts/{id}               account detail + status
DELETE /api/accounts/{id}               remove account + keychain cleanup
PATCH  /api/accounts/{id}               update balance or sync settings
POST   /api/accounts/{id}/sync          manual sync trigger
POST   /api/accounts/{id}/reconnect     reset to 'pending' for re-login
GET    /api/accounts/{id}/sync-log      background sync history
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile

ROOT = Path(__file__).resolve().parent.parent.parent
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.db import (
    add_account,
    delete_account,
    delete_session,
    get_account,
    get_accounts,
    get_sync_log,
    log_sync_end,
    log_sync_start,
    save_session,
    set_account_balance,
    set_account_status,
    set_auto_sync,
    set_last_url,
    set_needs_login,
    store_transactions,
    update_last_synced,
)

router = APIRouter()


def _period_to_since(period: str) -> datetime | None:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    mapping: dict[str, datetime | None] = {
        "1m":  today - timedelta(days=30),
        "3m":  today - timedelta(days=90),
        "6m":  today - timedelta(days=180),
        "1y":  today - timedelta(days=365),
        "ytd": today.replace(month=1, day=1),
        "all": None,
    }
    return mapping.get(period)


# ── Read ──────────────────────────────────────────────────────────────

@router.get("/accounts")
def list_accounts():
    return get_accounts()


@router.get("/accounts/{account_id}")
def account_detail(account_id: int):
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


# ── Delete ────────────────────────────────────────────────────────────

@router.delete("/accounts/{account_id}")
def remove_account(account_id: int):
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    # Clean up Plaid access token from keychain
    if account.get("source") == "plaid":
        try:
            import keyring
            keyring.delete_password("fifo_plaid", f"access_token_{account_id}")
        except Exception:
            pass
    delete_account(account_id)
    return {"ok": True}


# ── Patch ─────────────────────────────────────────────────────────────

@router.patch("/accounts/{account_id}")
def patch_account(account_id: int, body: dict = Body(...)):
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if "account_balance" in body:
        set_account_balance(account_id, body["account_balance"])
    if "auto_sync" in body or "sync_interval_hours" in body:
        set_auto_sync(
            account_id,
            enabled=body.get("auto_sync"),
            interval_hours=body.get("sync_interval_hours"),
        )
    return get_account(account_id)


# ── Connect (POST /api/accounts) ──────────────────────────────────────

@router.post("/accounts")
async def connect_account(
    body: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    username: Optional[str] = Form(None),
):
    """
    Three modes depending on what's posted:

    1. PDF file  → parse_bank PDF pipeline
    2. JSON {bank, period} without file → Playwright scraper (opens browser)
    3. Empty body → 400
    """
    data: dict = {}
    if body:
        try:
            data = json.loads(body)
        except Exception:
            pass

    # ── Mode 1: PDF upload ────────────────────────────────────────────
    if file and file.filename and file.filename.lower().endswith(".pdf"):
        pdf_bytes = await file.read()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = Path(tmp.name)
        try:
            from parse_bank import parse_pdf_to_payload, persist_uploaded_statement
            from categorize import categorize_transactions
            from new_bank_setup import bootstrap_config_if_unknown

            auto_bootstrap = (
                os.environ.get("AUTO_BOOTSTRAP_CONFIG", "1").strip().lower()
                not in {"0", "false", "no"}
            )
            bootstrap_result = None
            if auto_bootstrap:
                bootstrap_result = bootstrap_config_if_unknown(
                    tmp_path, config_dir=ROOT / "config" / "banks"
                )

            payload = parse_pdf_to_payload(tmp_path)
            payload["transactions"] = categorize_transactions(
                payload.get("transactions", [])
            )
            if bootstrap_result:
                payload["config_bootstrap"] = bootstrap_result

            try:
                storage = persist_uploaded_statement(
                    payload=payload,
                    original_filename=file.filename,
                    pdf_bytes=pdf_bytes,
                    username=username or "default",
                )
                payload["storage"] = storage
            except Exception as exc:
                payload["storage"] = {"error": str(exc)}

            return payload
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            tmp_path.unlink(missing_ok=True)

    # ── Mode 2: Scraper ───────────────────────────────────────────────
    bank_key = data.get("bank", "boa")
    period = data.get("period", "1y")
    since = _period_to_since(period) or (datetime.now() - timedelta(days=365))

    try:
        from bank_fetcher import scrape_recent_transactions, _load_fetcher_config
        from core.encryption import encrypt

        cfg = _load_fetcher_config(bank_key)
        display_name = cfg.get("bank_display_name", bank_key)
        account_id = add_account(bank_key, display_name, sync_period=period)

        sessions_dir = ROOT / "data" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_path = sessions_dir / f"{account_id}.json"

        result = scrape_recent_transactions(
            bank_key=bank_key,
            since_date=since,
            session_save_path=session_path,
        )

        # Handle different return signatures from bank_fetcher
        if isinstance(result, tuple) and len(result) == 4:
            txns, last_url, ending_balance, _ = result
        elif isinstance(result, tuple) and len(result) == 2:
            txns, last_url = result
            ending_balance = None
        else:
            txns, last_url, ending_balance = result, None, None

        # Encrypt + persist session cookie file
        if session_path.exists():
            enc = encrypt(session_path.read_text())
            expires = (datetime.now() + timedelta(days=30)).isoformat()
            save_session(account_id, enc, last_url, expires)
            session_path.unlink(missing_ok=True)

        from categorize import categorize_transactions
        categorized = categorize_transactions(txns) if txns else []
        store_transactions(account_id, categorized)
        update_last_synced(account_id, datetime.now())

        if ending_balance:
            set_account_balance(account_id, ending_balance)
        if last_url:
            set_last_url(account_id, last_url)
        set_account_status(account_id, "connected")

        return {
            "account":      get_account(account_id),
            "count":        len(categorized),
            "transactions": categorized,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Sync ──────────────────────────────────────────────────────────────

@router.post("/accounts/{account_id}/sync")
def sync_account(account_id: int):
    """Trigger a manual sync. Plaid accounts use the API; scraper accounts use headless session."""
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    source = account.get("source", "scraped")
    log_id = log_sync_start(account_id)
    set_account_status(account_id, "refreshing")

    try:
        if source == "plaid":
            from plaid_connector import fetch_transactions, fetch_balance
            last = account.get("last_synced_at")
            since = (
                datetime.fromisoformat(last)
                if last
                else datetime.now() - timedelta(days=365)
            )
            txns = fetch_transactions(account_id, since)
            added = store_transactions(account_id, txns)
            update_last_synced(account_id, datetime.now())
            bal = fetch_balance(account_id)
            if bal:
                set_account_balance(account_id, bal)
            set_account_status(account_id, "connected")
            log_sync_end(log_id, "ok", added)
            return {"count": added, "status": "connected"}

        else:
            # Scraper — attempt headless session replay
            bank_key = account.get("bank_key", "boa")
            last = account.get("last_synced_at")
            since = (
                datetime.fromisoformat(last)
                if last
                else datetime.now() - timedelta(days=90)
            )
            from connectors.scraper import ScraperConnector
            connector = ScraperConnector(bank_key)
            txns = connector.fetch_transactions(account_id, since)
            added = store_transactions(account_id, txns)
            update_last_synced(account_id, datetime.now())
            set_account_status(account_id, "connected")
            log_sync_end(log_id, "ok", added)
            return {"count": added, "status": "connected"}

    except Exception as exc:
        err = str(exc)
        is_auth_err = any(
            kw in err.lower()
            for kw in ("session", "expired", "login", "reauth", "unauthorized")
        )
        status = "reauth_required" if is_auth_err else "stale"
        set_account_status(account_id, status, err)
        log_sync_end(log_id, "error", 0, err)
        raise HTTPException(status_code=500, detail=err)


# ── Reconnect ─────────────────────────────────────────────────────────

@router.post("/accounts/{account_id}/reconnect")
def reconnect_account(account_id: int):
    """
    Reset the account to 'pending' so the user can log in again.
    Clears the stale session cookie from the DB.
    The frontend should then open the connect flow for this account.
    """
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    delete_session(account_id)
    set_account_status(account_id, "pending")
    set_needs_login(account_id, True)
    return {
        "status":  "pending",
        "message": "Session cleared. Use the connect flow to re-authenticate.",
    }


# ── Sync log ──────────────────────────────────────────────────────────

@router.get("/accounts/{account_id}/sync-log")
def account_sync_log(account_id: int, limit: int = 20):
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return get_sync_log(account_id, limit)
