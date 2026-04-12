"""
api/routers/plaid_router.py — Plaid Link + sync endpoints.

POST /api/plaid/create-link-token   — generate one-time Plaid Link token
POST /api/plaid/exchange-token      — swap public_token → access_token, fetch 1y
POST /api/plaid/sandbox-connect     — create sandbox item without Plaid Link
POST /api/plaid/sync/{id}           — delta-sync an existing Plaid account
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent.parent
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.db import (
    add_account, get_account, store_transactions,
    update_last_synced, set_account_balance, set_plaid_item_id,
    set_account_status, log_sync_start, log_sync_end,
)

router = APIRouter()


class ExchangeTokenBody(BaseModel):
    public_token: str
    institution_name: str = "Plaid Account"


@router.post("/plaid/create-link-token")
def create_link_token():
    try:
        from plaid_connector import create_link_token as _create
        return {"link_token": _create()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/plaid/exchange-token")
def exchange_token(body: ExchangeTokenBody):
    try:
        from plaid_connector import (
            exchange_public_token, save_access_token,
            fetch_transactions, fetch_balance,
        )
        access_token, item_id = exchange_public_token(body.public_token)
        account_id = add_account("plaid", body.institution_name, sync_period="1y", source="plaid")
        save_access_token(account_id, access_token)
        set_plaid_item_id(account_id, item_id)

        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=365)
        txns = fetch_transactions(account_id, since)
        store_transactions(account_id, txns)
        update_last_synced(account_id, datetime.now())

        bal = fetch_balance(account_id)
        if bal:
            set_account_balance(account_id, bal)

        set_account_status(account_id, "connected")
        return {
            "account":      get_account(account_id),
            "count":        len(txns),
            "transactions": txns,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/plaid/sandbox-connect")
def sandbox_connect():
    """
    Bypass Plaid Link entirely — creates a sandbox Chase item via the API.
    Useful when the Plaid CDN is unreachable (corporate networks, etc.).
    """
    try:
        from plaid_connector import (
            create_sandbox_item, save_access_token,
            fetch_transactions, fetch_balance,
        )
        access_token, item_id = create_sandbox_item()
        account_id = add_account("plaid", "Chase (Sandbox)", sync_period="1y", source="plaid")
        save_access_token(account_id, access_token)
        set_plaid_item_id(account_id, item_id)

        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=365)
        txns = fetch_transactions(account_id, since)
        store_transactions(account_id, txns)
        update_last_synced(account_id, datetime.now())

        bal = fetch_balance(account_id)
        if bal:
            set_account_balance(account_id, bal)

        set_account_status(account_id, "connected")
        return {
            "account":      get_account(account_id),
            "count":        len(txns),
            "transactions": txns,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/plaid/sync/{account_id}")
def plaid_sync(account_id: int):
    """Delta-sync a Plaid-connected account from its last_synced_at date."""
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    log_id = log_sync_start(account_id)
    set_account_status(account_id, "refreshing")
    try:
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
        return {"count": added}
    except Exception as exc:
        err = str(exc)
        set_account_status(account_id, "stale", err)
        log_sync_end(log_id, "error", 0, err)
        raise HTTPException(status_code=500, detail=err)
