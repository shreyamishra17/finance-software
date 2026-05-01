"""
db.py — Firestore persistence for FIFO accounts and transactions.

Collections:
  accounts     — one document per connected bank/investment account
  transactions — scraped transactions, deduplicated per account
  _meta        — internal counters + encryption key
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore

# ------------------------------------------------------------------ #
# Client init
# ------------------------------------------------------------------ #

_db = None
_SERVICE_ACCOUNT_PATH = (
    Path(__file__).resolve().parent.parent
    / "financesoftware-5c8e4-firebase-adminsdk-fbsvc-d456331984.json"
)


def _get_db():
    global _db
    if _db is not None:
        return _db
    if not firebase_admin._apps:
        if _SERVICE_ACCOUNT_PATH.exists():
            cred = credentials.Certificate(str(_SERVICE_ACCOUNT_PATH))
        else:
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    _db = fb_firestore.client()
    return _db


def init_db() -> None:
    """Connect to Firestore. Safe to call on every server start."""
    _get_db()


# ------------------------------------------------------------------ #
# ID generation — atomic counter stored in Firestore
# ------------------------------------------------------------------ #

def _next_account_id() -> int:
    db = _get_db()
    counter_ref = db.collection("_meta").document("counters")

    @fb_firestore.transactional
    def _increment(transaction, ref):
        snap = ref.get(transaction=transaction)
        current = int(snap.get("account_id_seq") or 0) if snap.exists else 0
        next_id = current + 1
        transaction.set(ref, {"account_id_seq": next_id}, merge=True)
        return next_id

    return _increment(db.transaction(), counter_ref)


# ------------------------------------------------------------------ #
# Accounts
# ------------------------------------------------------------------ #

def add_account(bank_key: str, display_name: str, sync_period: str = "1m") -> int:
    db = _get_db()
    existing = (
        db.collection("accounts")
        .where("bank_key", "==", bank_key)
        .where("display_name", "==", display_name)
        .limit(1)
        .get()
    )
    if existing:
        doc = existing[0]
        doc.reference.update({"sync_period": sync_period})
        return int(doc.get("id"))

    account_id = _next_account_id()
    now = datetime.now().isoformat()
    _get_db().collection("accounts").document(str(account_id)).set({
        "id":                  account_id,
        "bank_key":            bank_key,
        "display_name":        display_name,
        "account_hint":        None,
        "added_at":            now,
        "last_synced_at":      None,
        "sync_period":         sync_period,
        "auto_sync":           False,
        "sync_interval_hours": 24,
        "needs_login":         False,
        "last_url":            None,
        "account_balance":     None,
        "plaid_item_id":       None,
        "source":              "scraped",
    })
    return account_id


def get_accounts() -> list[dict]:
    db = _get_db()
    account_docs = (
        db.collection("accounts")
        .order_by("added_at", direction=fb_firestore.Query.DESCENDING)
        .get()
    )
    if not account_docs:
        return []
    tx_counts: dict[int, int] = {}
    for txn_doc in db.collection("transactions").get():
        aid = txn_doc.to_dict().get("account_id")
        if aid is not None:
            tx_counts[aid] = tx_counts.get(aid, 0) + 1
    result = []
    for doc in account_docs:
        data = doc.to_dict()
        data["tx_count"] = tx_counts.get(data.get("id"), 0)
        result.append(data)
    return result


def get_account(account_id: int) -> dict | None:
    doc = _get_db().collection("accounts").document(str(account_id)).get()
    return doc.to_dict() if doc.exists else None


def update_last_synced(account_id: int, dt: datetime, account_hint: str | None = None) -> None:
    updates: dict = {"last_synced_at": dt.isoformat()}
    if account_hint:
        updates["account_hint"] = account_hint
    _get_db().collection("accounts").document(str(account_id)).update(updates)


def delete_account(account_id: int) -> None:
    db = _get_db()
    txn_docs = db.collection("transactions").where("account_id", "==", account_id).get()
    batch = db.batch()
    count = 0
    for doc in txn_docs:
        batch.delete(doc.reference)
        count += 1
        if count >= 400:
            batch.commit()
            batch = db.batch()
            count = 0
    if count > 0:
        batch.commit()
    db.collection("accounts").document(str(account_id)).delete()


def set_auto_sync(account_id: int, enabled: bool | None = None, interval_hours: int | None = None) -> None:
    updates: dict = {}
    if enabled is not None:
        updates["auto_sync"] = bool(enabled)
    if interval_hours is not None:
        updates["sync_interval_hours"] = max(1, int(interval_hours))
    if updates:
        _get_db().collection("accounts").document(str(account_id)).update(updates)


def set_needs_login(account_id: int, flag: bool) -> None:
    _get_db().collection("accounts").document(str(account_id)).update({"needs_login": bool(flag)})


def set_last_url(account_id: int, url: str) -> None:
    _get_db().collection("accounts").document(str(account_id)).update({"last_url": url})


def set_account_balance(account_id: int, balance: str) -> None:
    _get_db().collection("accounts").document(str(account_id)).update({"account_balance": balance})


def set_plaid_item_id(account_id: int, item_id: str) -> None:
    _get_db().collection("accounts").document(str(account_id)).update(
        {"plaid_item_id": item_id, "source": "plaid"}
    )


# ------------------------------------------------------------------ #
# Transactions
# ------------------------------------------------------------------ #

def _normalize_date(raw: str) -> str:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


def _txn_doc_id(account_id: int, date: str, description: str, amount: str) -> str:
    key = f"{account_id}|{date}|{description}|{amount}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def store_transactions(account_id: int, transactions: list[dict]) -> int:
    db = _get_db()
    now = datetime.now().isoformat()
    inserted = 0
    batch = db.batch()
    batch_count = 0

    for txn in transactions:
        date = _normalize_date(txn.get("date", "") or "")
        description = txn.get("description", "")
        amount = txn.get("amount", "")
        doc_id = _txn_doc_id(account_id, date, description, amount)
        ref = db.collection("transactions").document(doc_id)

        existing = ref.get()
        if existing.exists:
            batch.update(ref, {
                "merchant_clean": txn.get("merchant_clean") or description,
                "category":       txn.get("category"),
            })
        else:
            batch.set(ref, {
                "id":             doc_id,
                "account_id":     account_id,
                "date":           date,
                "description":    description,
                "merchant_clean": txn.get("merchant_clean") or description,
                "amount":         amount,
                "amount_type":    txn.get("type", "debit"),
                "category":       txn.get("category"),
                "scraped_at":     now,
            })
            inserted += 1

        batch_count += 1
        if batch_count >= 400:
            batch.commit()
            batch = db.batch()
            batch_count = 0

    if batch_count > 0:
        batch.commit()
    return inserted


def update_transaction_categories(account_id: int, transactions: list[dict]) -> None:
    db = _get_db()
    batch = db.batch()
    batch_count = 0
    for txn in transactions:
        category = txn.get("category")
        merchant = txn.get("merchant_clean") or txn.get("description", "")
        if not category and not merchant:
            continue
        date = _normalize_date(txn.get("date", "") or "")
        doc_id = _txn_doc_id(account_id, date, txn.get("description", ""), txn.get("amount", ""))
        ref = db.collection("transactions").document(doc_id)
        batch.update(ref, {"category": category, "merchant_clean": merchant})
        batch_count += 1
        if batch_count >= 400:
            batch.commit()
            batch = db.batch()
            batch_count = 0
    if batch_count > 0:
        batch.commit()


def _normalize_date(raw: str) -> str | None:
    """Normalize any bank date string to YYYY-MM-DD for comparison.
    Returns None for unparseable strings like 'Pending' (those always pass the filter).
    Banks use many formats: MM/DD/YYYY (Capital One), YYYY-MM-DD (BofA), 'May 5' etc.
    """
    import re as _re
    s = raw.strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%b %d, %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # "May 5" or "May 05" — no year, assume current year
    m = _re.match(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})$', s, _re.I)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {datetime.now().year}", "%b %d %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None  # "Pending", "Processing", etc. → always include


def get_transactions(account_id: int | None = None, since_date: datetime | None = None) -> list[dict]:
    db = _get_db()
    query = db.collection("transactions")
    if account_id is not None:
        query = query.where("account_id", "==", account_id)
    docs = query.get()
    account_names: dict[int, str] = {}
    for acc_doc in db.collection("accounts").get():
        acc = acc_doc.to_dict()
        account_names[acc.get("id")] = acc.get("display_name", "")
    cutoff = since_date.strftime("%Y-%m-%d") if since_date else None
    result = []
    for doc in docs:
        data = doc.to_dict()
        raw_date = (data.get("date") or "").strip()
        if cutoff:
            iso = _normalize_date(raw_date)
            # Only exclude if we successfully parsed the date AND it's before the cutoff
            if iso and iso < cutoff:
                continue
            # If iso is None (Pending, etc.) → always include
        data["bank_name"] = account_names.get(data.get("account_id"), "")
        result.append(data)
    # Sort: pending/no-date float to top, real dates descending
    result.sort(
        key=lambda r: (_normalize_date(r.get("date") or "") or "9999-99-99", r.get("id") or ""),
        reverse=True,
    )
    return result


# ------------------------------------------------------------------ #
# Helpers used by serve_demo.py (replaces old _connect() raw SQL)
# ------------------------------------------------------------------ #

def get_transactions_for_account_raw(account_id: int) -> list[dict]:
    db = _get_db()
    docs = db.collection("transactions").where("account_id", "==", account_id).get()
    return [doc.to_dict() for doc in docs]


def get_latest_transaction_date(account_id: int) -> datetime | None:
    """Return the date of the most recently dated transaction stored for this account.

    Used to compute since_date for re-syncs so we never re-scrape more than
    1 day's worth of already-stored transactions, regardless of when the last
    sync actually ran.  Returns None if the account has no stored transactions.
    """
    db = _get_db()
    docs = db.collection("transactions").where("account_id", "==", account_id).get()
    latest_iso: str | None = None
    for doc in docs:
        raw = (doc.to_dict().get("date") or "").strip()
        iso = _normalize_date(raw)
        if iso and (latest_iso is None or iso > latest_iso):
            latest_iso = iso
    if latest_iso:
        try:
            return datetime.strptime(latest_iso, "%Y-%m-%d")
        except ValueError:
            pass
    return None


def get_uncategorized_transactions(limit: int = 5000) -> list[dict]:
    db = _get_db()
    all_docs = db.collection("transactions").limit(limit * 3).get()
    result = []
    for doc in all_docs:
        data = doc.to_dict()
        desc = data.get("description", "")
        merchant = data.get("merchant_clean") or ""
        if not data.get("category") or not merchant or merchant == desc:
            result.append(data)
            if len(result) >= limit:
                break
    return result


def update_transaction_by_id(txn_id: str, category: str | None, merchant_clean: str | None) -> None:
    _get_db().collection("transactions").document(txn_id).update({
        "category":       category,
        "merchant_clean": merchant_clean,
    })
