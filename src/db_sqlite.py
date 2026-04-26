"""
db_sqlite.py — SQLite persistence for FIFO (local development only).

Tables:
  accounts     — one row per connected bank/investment account
  transactions — scraped transactions, deduplicated per account
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "fifo.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_key       TEXT    NOT NULL,
    display_name   TEXT    NOT NULL,
    account_hint   TEXT,
    added_at       TEXT    NOT NULL,
    last_synced_at TEXT,
    sync_period    TEXT    NOT NULL DEFAULT '1m'
);

CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date         TEXT,
    description  TEXT,
    amount       TEXT,
    amount_type  TEXT,
    scraped_at   TEXT    NOT NULL,
    UNIQUE(account_id, date, description, amount)
);
"""


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_MIGRATIONS = [
    "ALTER TABLE accounts ADD COLUMN auto_sync INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE accounts ADD COLUMN sync_interval_hours INTEGER NOT NULL DEFAULT 24",
    "ALTER TABLE accounts ADD COLUMN needs_login INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE accounts ADD COLUMN last_url TEXT",
    "ALTER TABLE transactions ADD COLUMN category TEXT",
    "ALTER TABLE accounts ADD COLUMN account_balance TEXT",
    "ALTER TABLE transactions ADD COLUMN merchant_clean TEXT",
    "ALTER TABLE accounts ADD COLUMN plaid_item_id TEXT",
    "ALTER TABLE accounts ADD COLUMN source TEXT DEFAULT 'scraped'",
]


def init_db() -> None:
    """Create tables and run migrations. Safe to call on every server start."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except Exception:
                pass  # column already exists


# ------------------------------------------------------------------ #
# Accounts
# ------------------------------------------------------------------ #

def add_account(bank_key: str, display_name: str, sync_period: str = "1m") -> int:
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM accounts WHERE bank_key = ? AND display_name = ?",
            (bank_key, display_name),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE accounts SET sync_period = ? WHERE id = ?",
                (sync_period, existing["id"]),
            )
            return existing["id"]
        now = datetime.now().isoformat()
        cur = conn.execute(
            "INSERT INTO accounts (bank_key, display_name, added_at, sync_period) VALUES (?, ?, ?, ?)",
            (bank_key, display_name, now, sync_period),
        )
        return cur.lastrowid


def get_accounts() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("""
            SELECT a.id, a.bank_key, a.display_name, a.account_hint,
                   a.added_at, a.last_synced_at, a.sync_period,
                   COUNT(t.id) AS tx_count
            FROM accounts a
            LEFT JOIN transactions t ON t.account_id = a.id
            GROUP BY a.id
            ORDER BY a.added_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_account(account_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
    return dict(row) if row else None


def update_last_synced(account_id: int, dt: datetime, account_hint: str | None = None) -> None:
    with _connect() as conn:
        if account_hint:
            conn.execute(
                "UPDATE accounts SET last_synced_at = ?, account_hint = ? WHERE id = ?",
                (dt.isoformat(), account_hint, account_id),
            )
        else:
            conn.execute(
                "UPDATE accounts SET last_synced_at = ? WHERE id = ?",
                (dt.isoformat(), account_id),
            )


def delete_account(account_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


def set_auto_sync(
    account_id: int,
    enabled: bool | None = None,
    interval_hours: int | None = None,
) -> None:
    updates: list[str] = []
    params: list = []
    if enabled is not None:
        updates.append("auto_sync = ?")
        params.append(1 if enabled else 0)
    if interval_hours is not None:
        updates.append("sync_interval_hours = ?")
        params.append(max(1, int(interval_hours)))
    if not updates:
        return
    params.append(account_id)
    with _connect() as conn:
        conn.execute(f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?", params)


def set_needs_login(account_id: int, flag: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE accounts SET needs_login = ? WHERE id = ?",
            (1 if flag else 0, account_id),
        )


def set_last_url(account_id: int, url: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE accounts SET last_url = ? WHERE id = ?",
            (url, account_id),
        )


def set_account_balance(account_id: int, balance: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE accounts SET account_balance = ? WHERE id = ?",
            (balance, account_id),
        )


def set_plaid_item_id(account_id: int, item_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE accounts SET plaid_item_id = ?, source = 'plaid' WHERE id = ?",
            (item_id, account_id),
        )


# ------------------------------------------------------------------ #
# Transactions
# ------------------------------------------------------------------ #

def _normalize_date(raw: str) -> str:
    """Normalize transaction date to YYYY-MM-DD for correct comparisons."""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


def store_transactions(account_id: int, transactions: list[dict]) -> int:
    now = datetime.now().isoformat()
    inserted = 0
    with _connect() as conn:
        for txn in transactions:
            try:
                conn.execute(
                    """INSERT INTO transactions
                       (account_id, date, description, merchant_clean, amount, amount_type, category, scraped_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(account_id, date, description, amount)
                       DO UPDATE SET
                           merchant_clean = excluded.merchant_clean,
                           category       = excluded.category""",
                    (
                        account_id,
                        _normalize_date(txn.get("date", "") or ""),
                        txn.get("description", ""),
                        txn.get("merchant_clean") or txn.get("description", ""),
                        txn.get("amount", ""),
                        txn.get("type", "debit"),
                        txn.get("category"),
                        now,
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
            except Exception:
                pass
    return inserted


def update_transaction_categories(account_id: int, transactions: list[dict]) -> None:
    with _connect() as conn:
        for txn in transactions:
            category = txn.get("category")
            merchant = txn.get("merchant_clean") or txn.get("description", "")
            if not category and not merchant:
                continue
            conn.execute(
                """UPDATE transactions
                   SET category = ?, merchant_clean = ?
                   WHERE account_id = ?
                     AND date = ?
                     AND description = ?
                     AND amount = ?""",
                (
                    category,
                    merchant,
                    account_id,
                    _normalize_date(txn.get("date", "") or ""),
                    txn.get("description", ""),
                    txn.get("amount", ""),
                ),
            )


def get_transactions(
    account_id: int | None = None,
    since_date: datetime | None = None,
) -> list[dict]:
    conditions = []
    params: list = []
    if account_id is not None:
        conditions.append("t.account_id = ?")
        params.append(account_id)
    if since_date is not None:
        conditions.append("t.date >= ?")
        params.append(since_date.strftime("%Y-%m-%d"))
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT t.*, a.display_name AS bank_name
               FROM transactions t
               JOIN accounts a ON a.id = t.account_id
               {where}
               ORDER BY t.date DESC, t.id DESC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ #
# Helpers for serve_demo.py recategorize / backfill
# ------------------------------------------------------------------ #

def get_transactions_for_account_raw(account_id: int) -> list[dict]:
    """Return all transactions for an account with every stored field."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE account_id = ?", (account_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_uncategorized_transactions(limit: int = 5000) -> list[dict]:
    """Return transactions with missing category or un-cleaned merchant name."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT id, account_id, date, description, amount, amount_type,
                      category, merchant_clean, scraped_at
               FROM transactions
               WHERE category IS NULL
                  OR merchant_clean IS NULL
                  OR merchant_clean = description
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_transaction_by_id(
    txn_id,  # int for SQLite
    category: str | None,
    merchant_clean: str | None,
) -> None:
    """Update category and merchant_clean for a transaction by its ID."""
    with _connect() as conn:
        conn.execute(
            "UPDATE transactions SET category = ?, merchant_clean = ? WHERE id = ?",
            (category, merchant_clean, txn_id),
        )
