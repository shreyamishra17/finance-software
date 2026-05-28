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
import re
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Optional

import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore

# ------------------------------------------------------------------ #
# Client init
# ------------------------------------------------------------------ #

_db = None
_CURRENT_UID: ContextVar[str | None] = ContextVar("fifo_current_uid", default=None)
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


def set_current_uid(uid: str | None):
    return _CURRENT_UID.set(uid)


def reset_current_uid(token) -> None:
    _CURRENT_UID.reset(token)


def get_current_uid() -> str | None:
    return _CURRENT_UID.get()


def require_current_uid() -> str:
    uid = _CURRENT_UID.get()
    if not uid:
        raise RuntimeError("No authenticated FIFO user is set for this request")
    return uid


def _user_doc(uid: str | None = None):
    return _get_db().collection("users").document(uid or require_current_uid())


def _accounts_collection(uid: str | None = None):
    return _user_doc(uid).collection("accounts")


def _transactions_collection(uid: str | None = None):
    return _user_doc(uid).collection("transactions")


def _credentials_collection(uid: str | None = None):
    return _user_doc(uid).collection("credentials")


def _meta_collection(uid: str | None = None):
    return _user_doc(uid).collection("_meta")


def list_user_ids() -> list[str]:
    return [doc.id for doc in _get_db().collection("users").get()]


# ------------------------------------------------------------------ #
# ID generation — atomic counter stored in Firestore
# ------------------------------------------------------------------ #

def _next_account_id() -> int:
    db = _get_db()
    counter_ref = _meta_collection().document("counters")

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

def add_account(bank_key: str, display_name: str, sync_period: str = "1m", account_type: str = "checking") -> int:
    existing = (
        _accounts_collection()
        .where("bank_key", "==", bank_key)
        .where("display_name", "==", display_name)
        .limit(1)
        .get()
    )
    if existing:
        doc = existing[0]
        updates: dict = {"sync_period": sync_period}
        # Backfill account_type if it wasn't stored on first connect
        if not doc.to_dict().get("account_type"):
            updates["account_type"] = account_type
        doc.reference.update(updates)
        return int(doc.get("id"))

    account_id = _next_account_id()
    now = datetime.now().isoformat()
    _accounts_collection().document(str(account_id)).set({
        "id":                  account_id,
        "bank_key":            bank_key,
        "display_name":        display_name,
        "account_type":        account_type,
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
    """Return all accounts with their cached tx_count.

    We rely on a `tx_count` field stored on each account doc — updated
    incrementally by store_transactions(). Scanning the whole transactions
    collection on every /api/accounts call was burning ~1500 reads per call
    and hit Firestore's free-tier daily quota in ~30 calls.
    """
    account_docs = (
        _accounts_collection()
        .order_by("added_at", direction=fb_firestore.Query.DESCENDING)
        .get()
    )
    if not account_docs:
        return []
    return [_account_with_count(doc.to_dict()) for doc in account_docs]


def _account_with_count(data: dict) -> dict:
    data.setdefault("tx_count", 0)
    return data


def get_account(account_id: int) -> dict | None:
    doc = _accounts_collection().document(str(account_id)).get()
    return doc.to_dict() if doc.exists else None


def set_account_type(account_id: int, account_type: str) -> None:
    """Set / correct the account_type field (e.g. 'credit', 'checking')."""
    _accounts_collection().document(str(account_id)).update({"account_type": account_type})


def update_last_synced(account_id: int, dt: datetime, account_hint: str | None = None) -> None:
    updates: dict = {"last_synced_at": dt.isoformat()}
    if account_hint:
        updates["account_hint"] = account_hint
    _accounts_collection().document(str(account_id)).update(updates)


def delete_account(account_id: int) -> None:
    db = _get_db()
    txn_docs = _transactions_collection().where("account_id", "==", account_id).get()
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
    _accounts_collection().document(str(account_id)).delete()


def set_auto_sync(account_id: int, enabled: bool | None = None, interval_hours: int | None = None) -> None:
    updates: dict = {}
    if enabled is not None:
        updates["auto_sync"] = bool(enabled)
    if interval_hours is not None:
        updates["sync_interval_hours"] = max(1, int(interval_hours))
    if updates:
        _accounts_collection().document(str(account_id)).update(updates)


def set_needs_login(account_id: int, flag: bool) -> None:
    _accounts_collection().document(str(account_id)).update({"needs_login": bool(flag)})


def set_account_status(account_id: int, status: str, error: str | None = None) -> None:
    _accounts_collection().document(str(account_id)).update(
        {"status": status, "last_error": error}
    )


def set_last_url(account_id: int, url: str) -> None:
    _accounts_collection().document(str(account_id)).update({"last_url": url})


def set_has_credentials(account_id: int, flag: bool) -> None:
    """Persist whether the user has saved bank credentials for this account.
    Stored on the account doc so the GET /api/accounts handler doesn't have
    to probe the OS keychain on every request (which spams macOS prompts)."""
    _accounts_collection().document(str(account_id)).update({"has_credentials": bool(flag)})


def set_account_balance(account_id: int, balance: str) -> None:
    _accounts_collection().document(str(account_id)).update({"account_balance": balance})


def set_plaid_item_id(account_id: int, item_id: str) -> None:
    _accounts_collection().document(str(account_id)).update(
        {"plaid_item_id": item_id, "source": "plaid"}
    )


def set_investment_balance(account_id: int, balance: str) -> None:
    """Store the investment portfolio value (e.g. mutual funds) scraped from the bank overview."""
    _accounts_collection().document(str(account_id)).update({"investment_balance": balance})


# ------------------------------------------------------------------ #
# Transactions
# ------------------------------------------------------------------ #

def _normalize_date_store(raw: str) -> str:
    """Normalize date for storage — returns raw string unchanged if format is unrecognised.
    Handles ISO dates, Indian bank formats, and multi-date cells (e.g. ICICI "23 Apr 2026 20 Apr 2026").
    """
    s = (raw or "").strip()
    if not s:
        return s
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d %b %Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Multi-date cell like "23 Apr 2026 20 Apr 2026" — extract the first date token
    _DATE_RE = re.compile(
        r'\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}'
        r'|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?',
        re.IGNORECASE,
    )
    m = _DATE_RE.search(s)
    if m:
        candidate = m.group(0).strip()
        if candidate != s:
            return _normalize_date_store(candidate)
    return s


def _txn_doc_id(account_id: int, date: str, description: str, amount: str, balance: str = "") -> str:
    key = f"{account_id}|{date}|{description}|{amount}|{balance}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _parse_amount_float(amt_str: str) -> float | None:
    """Parse a raw amount string like '$159.47' or '-$94.40' into a float."""
    s = re.sub(r"[^\d.\-]", "", (amt_str or "").strip())
    try:
        return float(s)
    except ValueError:
        return None


def store_transactions(account_id: int, transactions: list[dict]) -> int:
    db = _get_db()
    now = datetime.now().isoformat()
    inserted = 0
    batch = db.batch()
    batch_count = 0

    for txn in transactions:
        date = _normalize_date_store(txn.get("date", "") or "")
        description = txn.get("description", "")
        amount = txn.get("amount", "")
        doc_id = _txn_doc_id(account_id, date, description, amount, txn.get("balance", ""))
        ref = _transactions_collection().document(doc_id)

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

    # Keep the cached tx_count on the account doc in sync. Cheap (single
    # write) and avoids scanning the transactions collection on every
    # /api/accounts read.
    if inserted > 0:
        try:
            _accounts_collection().document(str(account_id)).update({
                "tx_count": fb_firestore.Increment(inserted),
            })
        except Exception as exc:
            print(f"WARNING: failed to update tx_count for account {account_id}: {exc}")

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
        date = _normalize_date_store(txn.get("date", "") or "")
        doc_id = _txn_doc_id(account_id, date, txn.get("description", ""), txn.get("amount", ""))
        ref = _transactions_collection().document(doc_id)
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
    Also handles ICICI multi-date cells like "23 Apr 2026 20 Apr 2026".
    """
    s = raw.strip() if raw else ""
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%b %d, %Y", "%b %d %Y",
                "%d %b %Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # "May 5" or "May 05" — no year, assume current year
    m = re.match(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})$', s, re.I)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {datetime.now().year}", "%b %d %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Multi-date cell like "23 Apr 2026 20 Apr 2026" — extract the first date token
    m2 = re.search(
        r'\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})',
        s, re.IGNORECASE
    )
    if m2 and m2.group(1).strip() != s:
        return _normalize_date(m2.group(1).strip())
    return None  # "Pending", "Processing", etc. → always include


def get_transactions(account_id: int | None = None, since_date: datetime | None = None) -> list[dict]:
    query = _transactions_collection()
    if account_id is not None:
        query = query.where("account_id", "==", account_id)
    docs = query.get()
    account_names: dict[int, str] = {}
    for acc_doc in _accounts_collection().get():
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
    docs = _transactions_collection().where("account_id", "==", account_id).get()
    return [doc.to_dict() for doc in docs]


def get_latest_transaction_date(account_id: int) -> datetime | None:
    """Return the date of the most recently dated transaction stored for this account.

    Used to compute since_date for re-syncs so we never re-scrape more than
    1 day's worth of already-stored transactions, regardless of when the last
    sync actually ran.  Returns None if the account has no stored transactions.
    """
    docs = _transactions_collection().where("account_id", "==", account_id).get()
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
    all_docs = _transactions_collection().limit(limit * 3).get()
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


def update_transaction_by_id(
    txn_id: str,
    category: str | None,
    merchant_clean: str | None,
    amount_type: str | None = None,
) -> None:
    updates: dict = {
        "category":       category,
        "merchant_clean": merchant_clean,
    }
    if amount_type is not None:
        updates["amount_type"] = amount_type
    _transactions_collection().document(txn_id).update(updates)
