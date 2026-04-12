"""
api/routers/transactions.py — Transaction endpoints.

GET  /api/accounts/{id}/transactions  — paginated transaction list
POST /api/accounts/{id}/recategorize  — re-run categorization
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

ROOT = Path(__file__).resolve().parent.parent.parent
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.db import get_account, get_transactions, update_transaction_categories

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


@router.get("/accounts/{account_id}/transactions")
def list_transactions(
    account_id: int,
    period: str = Query("all"),
):
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    since = _period_to_since(period)
    return get_transactions(account_id, since_date=since)


@router.post("/accounts/{account_id}/recategorize")
def recategorize(account_id: int):
    """Re-run the full categorization pipeline on stored transactions."""
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    txns = get_transactions(account_id)
    if not txns:
        return {"updated": 0}

    try:
        from categorize import categorize_transactions
        categorized = categorize_transactions(txns)
        update_transaction_categories(account_id, categorized)
        return {"updated": len(categorized)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
