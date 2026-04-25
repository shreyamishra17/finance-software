"""
api/routers/import_router.py — Bookmarklet import endpoint.

POST /api/import   — receive scraped transactions from the browser bookmarklet
                     and store them (creating the account if needed).

Expected body:
{
  "bank_key":     "boa" | "capital_one" | "discover" | ...,
  "transactions": [
    { "date": "2026-01-15", "description": "Starbucks", "amount": "-4.50",
      "amount_type": "debit" },
    ...
  ]
}

Returns: { "count": <newly inserted>, "account_id": <int> }
"""
from __future__ import annotations

import sys
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
    update_last_synced, set_account_status, update_transaction_categories,
)
from datetime import datetime

router = APIRouter()

# Human-readable bank names by key
_BANK_NAMES: dict[str, str] = {
    "boa":         "Bank of America",
    "capital_one": "Capital One",
    "discover":    "Discover",
    "chase":       "Chase",
    "wells_fargo": "Wells Fargo",
    "citi":        "Citi",
    "amex":        "American Express",
    "usbank":      "US Bank",
}


class ImportBody(BaseModel):
    bank_key: str
    transactions: list[dict[str, Any]]


@router.post("/import")
def import_transactions(body: ImportBody):
    """
    Receive transactions scraped by the FIFO browser bookmarklet.
    Creates the account on first import; subsequent imports upsert only new rows.
    """
    bank_key    = body.bank_key.lower().strip()
    display_name = _BANK_NAMES.get(bank_key, bank_key.replace("_", " ").title())

    try:
        # Create (or reuse) the account
        account_id = add_account(
            bank_key     = bank_key,
            display_name = display_name,
            sync_period  = "1y",
            source       = "bookmarklet",
        )

        # Categorize before storing
        txns = body.transactions
        try:
            from categorize import categorize_transactions
            txns = categorize_transactions(txns)
        except Exception:
            pass  # categorization is best-effort

        count = store_transactions(account_id, txns)
        update_last_synced(account_id, datetime.now())
        set_account_status(account_id, "connected")

        return {
            "count":      count,
            "account_id": account_id,
            "total":      len(txns),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
