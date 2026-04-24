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

# Keywords that identify a payment TO a credit card (not a merchant charge or refund)
_PAYMENT_KEYWORDS = frozenset({
    "payment", "autopay", "auto pay", "online payment",
    "mobile payment", "ach payment",
})


def _parse_date(raw: str) -> datetime | None:
    """Parse dates in multiple formats including Capital One's 'Mon DD' (no year)."""
    raw = " ".join(raw.strip().split())  # normalise whitespace / newlines
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    # "Apr 02" — infer year: current year, or last year if that date is in the future
    try:
        dt = datetime.strptime(raw, "%b %d")
        now = datetime.now()
        candidate = dt.replace(year=now.year)
        if candidate > now:
            candidate = candidate.replace(year=now.year - 1)
        return candidate
    except ValueError:
        pass
    return None


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


def _is_payment_txn(description: str) -> bool:
    """True if this is a payment toward the credit card balance, not a merchant charge."""
    d = (description or "").lower()
    return any(kw in d for kw in _PAYMENT_KEYWORDS)


@router.get("/accounts/{account_id}/insights")
def get_insights(
    account_id: int,
    period: str = Query("all"),
):
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    is_credit = (account.get("account_type") or "checking") == "credit"
    since = _period_to_since(period)
    txns = get_transactions(account_id, since_date=since)

    if is_credit:
        return _credit_insights(account, txns)
    else:
        return _checking_insights(account, txns)


def _credit_insights(account: dict, txns: list[dict]) -> dict:
    """
    Credit card insight calculation.
    Capital One (and most credit cards) stores:
      positive amount = charge (money out)
      negative amount = payment or refund (money in)
    """
    total_charges   = 0.0
    total_payments  = 0.0
    by_category: dict[str, dict] = {}
    merchant_totals: dict[str, float] = {}

    for t in txns:
        raw = (t.get("amount") or "").replace("$", "").replace(",", "").strip()
        try:
            amt = float(raw)
        except ValueError:
            continue

        desc = t.get("merchant_clean") or t.get("description") or ""
        cat  = t.get("category") or "Uncategorized"

        if amt > 0:
            # Charge — counts toward spending
            total_charges += amt
            if cat not in by_category:
                by_category[cat] = {"category": cat, "total": 0.0, "count": 0}
            by_category[cat]["total"] = round(by_category[cat]["total"] + amt, 2)
            by_category[cat]["count"] += 1
            # Only real merchants in top merchants (no payment keywords)
            if not _is_payment_txn(desc):
                merchant_totals[desc] = round(merchant_totals.get(desc, 0.0) + amt, 2)

        elif amt < 0:
            # Payment or refund
            if _is_payment_txn(desc):
                total_payments += abs(amt)
                # Payments don't go into category or merchant lists

    top_merchants = sorted(
        [{"name": k, "total": v} for k, v in merchant_totals.items()],
        key=lambda x: x["total"],
        reverse=True,
    )[:10]

    unpaid = round(total_charges - total_payments, 2)

    return {
        "account_type":    "credit",
        "total_charges":   round(total_charges, 2),
        "total_payments":  round(total_payments, 2),
        "unpaid_charges":  unpaid,
        # Keep legacy keys so any existing code doesn't break
        "total_income":    round(total_payments, 2),
        "total_expenses":  round(total_charges, 2),
        "net":             round(total_payments - total_charges, 2),
        "current_balance": account.get("account_balance", "") or "",
        "by_category": sorted(
            by_category.values(), key=lambda x: x["total"], reverse=True
        ),
        "top_merchants": top_merchants,
    }


def _checking_insights(account: dict, txns: list[dict]) -> dict:
    """Checking/savings account insight calculation (original logic)."""
    total_income    = 0.0
    total_expenses  = 0.0
    by_category: dict[str, dict] = {}
    merchant_totals: dict[str, float] = {}

    for t in txns:
        raw = (t.get("amount") or "").replace("$", "").replace(",", "").strip()
        try:
            amt = float(raw)
        except ValueError:
            continue

        cat   = t.get("category") or "Uncategorized"
        atype = (t.get("amount_type") or t.get("type") or "debit").lower()

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
            merchant_totals[name] = round(merchant_totals.get(name, 0.0) + abs(amt), 2)

    top_merchants = sorted(
        [{"name": k, "total": v} for k, v in merchant_totals.items()],
        key=lambda x: x["total"],
        reverse=True,
    )[:10]

    return {
        "account_type":   "checking",
        "total_income":   round(total_income, 2),
        "total_expenses": round(total_expenses, 2),
        "net":            round(total_income - total_expenses, 2),
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

    is_credit = (account.get("account_type") or "checking") == "credit"

    since = (
        datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=30 * months)
    )
    txns = get_transactions(account_id, since_date=since)

    by_month: dict[str, dict] = {}
    for t in txns:
        dt = _parse_date(t.get("date") or "")
        if dt is None:
            continue

        key = dt.strftime("%Y-%m")
        if key not in by_month:
            by_month[key] = {"month": key, "expenses": 0.0, "income": 0.0,
                             "charges": 0.0, "payments": 0.0}

        raw = (t.get("amount") or "").replace("$", "").replace(",", "").strip()
        try:
            amt = float(raw)
        except ValueError:
            continue

        cat  = t.get("category") or "Uncategorized"
        desc = t.get("merchant_clean") or t.get("description") or ""

        if is_credit:
            if amt > 0:
                by_month[key]["charges"]  = round(by_month[key]["charges"]  + amt, 2)
                by_month[key]["expenses"] = round(by_month[key]["expenses"] + amt, 2)
            elif amt < 0 and _is_payment_txn(desc):
                by_month[key]["payments"] = round(by_month[key]["payments"] + abs(amt), 2)
                by_month[key]["income"]   = round(by_month[key]["income"]   + abs(amt), 2)
        else:
            if cat in _TRANSFER_CATS:
                continue
            atype = (t.get("amount_type") or t.get("type") or "debit").lower()
            if atype == "credit":
                by_month[key]["income"]   = round(by_month[key]["income"]   + abs(amt), 2)
            else:
                by_month[key]["expenses"] = round(by_month[key]["expenses"] + abs(amt), 2)

    return {
        "account_type": "credit" if is_credit else "checking",
        "months": sorted(by_month.values(), key=lambda x: x["month"]),
    }
