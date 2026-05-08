"""
api/routers/transactions.py — Transaction endpoints.

GET  /api/accounts/{id}/transactions  — paginated transaction list
POST /api/accounts/{id}/recategorize  — re-run categorization
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
import json
import re
import sqlite3

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent.parent
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.db import get_account, get_transactions, update_transaction_categories


router = APIRouter()

_DB_PATH = ROOT / "data" / "fifo.db"
_OVERRIDE_PATH = ROOT / "data" / "manual_category_overrides.json"

KNOWN_CATEGORIES = [
    "Auto & Transport", "Entertainment", "Finance", "Food & Drink", "Government & Legal",
    "Groceries", "Health", "Housing", "Income", "Miscellaneous", "Money Transfer",
    "Personal Care", "Shopping", "Travel", "Uncategorized", "Utilities & Bills",
]


class CategoryUpdateBody(BaseModel):
    category: str


def _normalize_merchant_key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(text or "").upper()).strip()


def _load_overrides() -> dict[str, dict[str, str]]:
    if not _OVERRIDE_PATH.exists():
        return {}
    try:
        data = json.loads(_OVERRIDE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_overrides(data: dict[str, dict[str, str]]) -> None:
    _OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OVERRIDE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))


def _apply_override_to_txn(txn: dict, overrides: dict[str, dict[str, str]]) -> dict:
    merchant = txn.get("merchant_clean") or txn.get("description") or ""
    key = _normalize_merchant_key(merchant)
    override = overrides.get(key)
    if override and override.get("category"):
        txn = dict(txn)
        txn["category"] = override["category"]
    return txn


def _update_transaction_and_matching_merchants(transaction_id: int, category: str) -> dict:
    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, merchant_clean, description FROM transactions WHERE id = ?",
            (transaction_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Transaction not found")

        merchant = row["merchant_clean"] or row["description"] or ""
        key = _normalize_merchant_key(merchant)
        if not key:
            raise HTTPException(status_code=400, detail="Merchant key unavailable for override")

        conn.execute("UPDATE transactions SET category = ? WHERE id = ?", (category, transaction_id))

        updated = 1
        rows = conn.execute("SELECT id, merchant_clean, description FROM transactions").fetchall()
        for txn in rows:
            candidate = txn["merchant_clean"] or txn["description"] or ""
            if _normalize_merchant_key(candidate) != key or txn["id"] == transaction_id:
                continue
            conn.execute("UPDATE transactions SET category = ? WHERE id = ?", (category, txn["id"]))
            updated += 1
        conn.commit()

    overrides = _load_overrides()
    overrides[key] = {"merchant": merchant, "category": category}
    _save_overrides(overrides)
    return {"updated": updated, "merchant": merchant, "category": category}

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
    txns = get_transactions(account_id, since_date=since)
    overrides = _load_overrides()
    return [_apply_override_to_txn(txn, overrides) for txn in txns]


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
        overrides = _load_overrides()
        for txn in categorized:
            override = overrides.get(_normalize_merchant_key(txn.get("merchant_clean") or txn.get("description") or ""))
            if override and override.get("category"):
                txn["category"] = override["category"]
        update_transaction_categories(account_id, categorized)
        return {"updated": len(categorized)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/categories")
def list_categories():
    return {"categories": KNOWN_CATEGORIES}


@router.patch("/transactions/{transaction_id}/category")
def update_transaction_category(transaction_id: int, body: CategoryUpdateBody):
    category = str(body.category or "").strip()
    if not category:
        raise HTTPException(status_code=400, detail="Category is required")
    return _update_transaction_and_matching_merchants(transaction_id, category)
