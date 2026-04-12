"""
api/routers/insights.py — Analytics endpoints.

GET /api/accounts/{id}/insights  — totals, category breakdown, top merchants
GET /api/accounts/{id}/monthly   — income/expense grouped by YYYY-MM
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

from core.db import get_account, get_transactions

router = APIRouter()

_TRANSFER_CATS = {"Transfer", "Debt Payments"}


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


@router.get("/accounts/{account_id}/insights")
def get_insights(
    account_id: int,
    period: str = Query("all"),
):
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    since = _period_to_since(period)
    txns = get_transactions(account_id, since_date=since)

    total_income = 0.0
    total_expenses = 0.0
    by_category: dict[str, dict] = {}
    merchant_totals: dict[str, float] = {}

    for t in txns:
        raw = (t.get("amount") or "").replace("$", "").replace(",", "").strip()
        try:
            amt = float(raw)
        except ValueError:
            continue

        cat = t.get("category") or "Uncategorized"
        atype = (t.get("amount_type") or t.get("type") or "debit").lower()

        # Skip transfers — they inflate both income and expenses
        if cat in _TRANSFER_CATS:
            continue

        if atype == "credit":
            total_income += abs(amt)
        else:
            total_expenses += abs(amt)

        if cat not in by_category:
            by_category[cat] = {"category": cat, "total": 0.0, "count": 0}
        by_category[cat]["total"] = round(by_category[cat]["total"] + abs(amt), 2)
        by_category[cat]["count"] += 1

        if atype != "credit":
            name = t.get("merchant_clean") or t.get("description") or "Unknown"
            merchant_totals[name] = round(
                merchant_totals.get(name, 0.0) + abs(amt), 2
            )

    top_merchants = sorted(
        [{"name": k, "total": v} for k, v in merchant_totals.items()],
        key=lambda x: x["total"],
        reverse=True,
    )[:10]

    return {
        "total_income":    round(total_income, 2),
        "total_expenses":  round(total_expenses, 2),
        "net":             round(total_income - total_expenses, 2),
        "current_balance": account.get("account_balance", "") or "",
        "by_category": sorted(
            by_category.values(), key=lambda x: x["total"]
        ),
        "top_merchants": top_merchants,
    }


@router.get("/accounts/{account_id}/monthly")
def get_monthly(
    account_id: int,
    months: int = Query(12, ge=1, le=60),
):
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    since = (
        datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=30 * months)
    )
    txns = get_transactions(account_id, since_date=since)

    by_month: dict[str, dict] = {}
    for t in txns:
        raw_date = (t.get("date") or "").strip()
        try:
            dt = datetime.strptime(raw_date, "%Y-%m-%d")
        except ValueError:
            try:
                dt = datetime.strptime(raw_date, "%m/%d/%Y")
            except ValueError:
                continue

        key = dt.strftime("%Y-%m")
        if key not in by_month:
            by_month[key] = {"month": key, "expenses": 0.0, "income": 0.0}

        raw = (t.get("amount") or "").replace("$", "").replace(",", "").strip()
        try:
            amt = float(raw)
        except ValueError:
            continue

        cat = t.get("category") or "Uncategorized"
        if cat in _TRANSFER_CATS:
            continue

        atype = (t.get("amount_type") or t.get("type") or "debit").lower()
        if atype == "credit":
            by_month[key]["income"] = round(by_month[key]["income"] + abs(amt), 2)
        else:
            by_month[key]["expenses"] = round(by_month[key]["expenses"] + abs(amt), 2)

    return {"months": sorted(by_month.values(), key=lambda x: x["month"])}
