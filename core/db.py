"""
core/db.py — Primary database module for FIFO.

Superset of src/db.py — shares the same data/fifo.db file.
New tables: account_sessions (encrypted browser state), sync_log.
New column: accounts.status (connection state machine).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "fifo.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_key            TEXT    NOT NULL,
    display_name        TEXT    NOT NULL,
    account_hint        TEXT,
    added_at            TEXT    NOT NULL,
    last_synced_at      TEXT,
    sync_period         TEXT    NOT NULL DEFAULT '1m',
    auto_sync           INTEGER NOT NULL DEFAULT 0,
    sync_interval_hours INTEGER NOT NULL DEFAULT 8,
    needs_login         INTEGER NOT NULL DEFAULT 0,
    last_url            TEXT,
    account_balance     TEXT,
    plaid_item_id       TEXT,
    source              TEXT    DEFAULT 'scraped',
    account_type        TEXT    DEFAULT 'checking',
    status              TEXT    NOT NULL DEFAULT 'connected',
    last_error          TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date            TEXT,
    description     TEXT,
    merchant_clean  TEXT,
    amount          TEXT,
    amount_type     TEXT,
    category        TEXT,
    scraped_at      TEXT    NOT NULL,
    UNIQUE(account_id, date, description, amount)
);

CREATE TABLE IF NOT EXISTS account_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    session_data_enc TEXT,
    session_url      TEXT,
    session_expires  TEXT,
    created_at       TEXT    NOT NULL,
    UNIQUE(account_id)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    started_at      TEXT    NOT NULL,
    completed_at    TEXT,
    status          TEXT    NOT NULL DEFAULT 'running',
    tx_added        INTEGER DEFAULT 0,
    error_message   TEXT
);
"""

# Additive migrations — safe to run on existing databases
_MIGRATIONS = [
    "ALTER TABLE accounts ADD COLUMN auto_sync INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE accounts ADD COLUMN sync_interval_hours INTEGER NOT NULL DEFAULT 8",
    "ALTER TABLE accounts ADD COLUMN needs_login INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE accounts ADD COLUMN last_url TEXT",
    "ALTER TABLE transactions ADD COLUMN category TEXT",
    "ALTER TABLE accounts ADD COLUMN account_balance TEXT",
    "ALTER TABLE transactions ADD COLUMN merchant_clean TEXT",
    "ALTER TABLE accounts ADD COLUMN plaid_item_id TEXT",
    "ALTER TABLE accounts ADD COLUMN source TEXT DEFAULT 'scraped'",
    "ALTER TABLE accounts ADD COLUMN account_type TEXT DEFAULT 'checking'",
    "ALTER TABLE accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'connected'",
    "ALTER TABLE accounts ADD COLUMN last_error TEXT",
]


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create tables, run migrations, and deduplicate existing rows on every startup."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except Exception:
                pass  # column already exists
    deduplicate_accounts()


def deduplicate_accounts() -> int:
    """
    One-time cleanup (safe to call repeatedly): for each group of accounts
    sharing the same (bank_key, display_name), keep the row with the most
    recent last_synced_at and migrate all transactions to it, then delete
    the duplicates.

    Returns number of duplicate rows removed.
    """
    conn = _connect()
    try:
        dupes = conn.execute("""
            SELECT bank_key, display_name, COUNT(*) AS cnt
            FROM accounts
            GROUP BY bank_key, display_name
            HAVING cnt > 1
        """).fetchall()

        deleted = 0
        for dupe in dupes:
            bk, dn = dupe["bank_key"], dupe["display_name"]
            rows = conn.execute("""
                SELECT a.id,
                       a.last_synced_at,
                       (SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.id) AS tx_count
                FROM accounts a
                WHERE a.bank_key = ? AND a.display_name = ?
                ORDER BY
                    CASE WHEN a.last_synced_at IS NULL THEN 0 ELSE 1 END DESC,
                    a.last_synced_at DESC,
                    tx_count DESC
            """, (bk, dn)).fetchall()

            if len(rows) < 2:
                continue

            winner_id = rows[0]["id"]
            loser_ids = [r["id"] for r in rows[1:]]

            # Migrate unique transactions from losers to winner
            for loser_id in loser_ids:
                conn.execute("""
                    INSERT OR IGNORE INTO transactions
                    (account_id, date, description, merchant_clean,
                     amount, amount_type, category, scraped_at)
                    SELECT ?, date, description, merchant_clean,
                           amount, amount_type, category, scraped_at
                    FROM transactions WHERE account_id = ?
                """, (winner_id, loser_id))

            placeholders = ",".join("?" * len(loser_ids))
            conn.execute(
                f"DELETE FROM accounts WHERE id IN ({placeholders})", loser_ids
            )
            deleted += len(loser_ids)

        conn.commit()
        if deleted:
            print(f"[db] Removed {deleted} duplicate account row(s).")
        return deleted
    finally:
        conn.close()


# ── Accounts ──────────────────────────────────────────────────────────

def add_account(
    bank_key: str,
    display_name: str,
    sync_period: str = "1m",
    source: str = "scraped",
    account_type: str = "checking",
) -> int:
    """
    Insert a new account row, or return the existing row id if
    (bank_key, display_name) already exists — prevents ghost duplicates.
    """
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM accounts WHERE bank_key = ? AND display_name = ?",
            (bank_key, display_name),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE accounts SET source = ?, sync_period = ?, account_type = ? WHERE id = ?",
                (source, sync_period, account_type, existing["id"]),
            )
            return existing["id"]
        now = datetime.now().isoformat()
        cur = conn.execute(
            """INSERT INTO accounts
               (bank_key, display_name, added_at, sync_period, source, account_type, status)
               VALUES (?, ?, ?, ?, ?, ?, 'connected')""",
            (bank_key, display_name, now, sync_period, source, account_type),
        )
        return cur.lastrowid


def get_accounts() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("""
            SELECT a.id, a.bank_key, a.display_name, a.account_hint,
                   a.added_at, a.last_synced_at, a.sync_period,
                   a.auto_sync, a.sync_interval_hours, a.needs_login,
                   a.account_balance, a.source, a.account_type, a.status, a.last_error,
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


def update_last_synced(
    account_id: int, dt: datetime, account_hint: str | None = None
) -> None:
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


def set_account_status(
    account_id: int, status: str, error: str | None = None
) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE accounts SET status = ?, last_error = ? WHERE id = ?",
            (status, error, account_id),
        )


def delete_account(account_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


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


def set_needs_login(account_id: int, flag: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE accounts SET needs_login = ? WHERE id = ?",
            (1 if flag else 0, account_id),
        )


def set_last_url(account_id: int, url: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE accounts SET last_url = ? WHERE id = ?", (url, account_id)
        )


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


# ── Transactions ──────────────────────────────────────────────────────

def _normalize_date(raw: str) -> str:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


def store_transactions(account_id: int, transactions: list[dict]) -> int:
    """
    Upsert transactions. Returns count of newly inserted rows.
    Duplicate key: (account_id, date, description, amount).
    On conflict: update merchant_clean and category only.
    """
    now = datetime.now().isoformat()
    inserted = 0
    with _connect() as conn:
        for txn in transactions:
            try:
                conn.execute(
                    """INSERT INTO transactions
                       (account_id, date, description, merchant_clean,
                        amount, amount_type, category, scraped_at)
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
                        txn.get("amount_type") or txn.get("type", "debit"),
                        txn.get("category"),
                        now,
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
            except Exception:
                pass
    return inserted


def update_transaction_categories(
    account_id: int, transactions: list[dict]
) -> None:
    with _connect() as conn:
        for txn in transactions:
            category = txn.get("category")
            merchant = txn.get("merchant_clean") or txn.get("description", "")
            if not category and not merchant:
                continue
            conn.execute(
                """UPDATE transactions
                   SET category = ?, merchant_clean = ?
                   WHERE account_id = ? AND date = ? AND description = ? AND amount = ?""",
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
    conditions: list[str] = []
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


# ── Account Sessions ──────────────────────────────────────────────────

def save_session(
    account_id: int,
    session_data_enc: str,
    session_url: str | None = None,
    expires_at: str | None = None,
) -> None:
    now = datetime.now().isoformat()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO account_sessions
               (account_id, session_data_enc, session_url, session_expires, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                   session_data_enc = excluded.session_data_enc,
                   session_url      = excluded.session_url,
                   session_expires  = excluded.session_expires,
                   created_at       = excluded.created_at""",
            (account_id, session_data_enc, session_url, expires_at, now),
        )


def load_session(account_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM account_sessions WHERE account_id = ?", (account_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_session(account_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM account_sessions WHERE account_id = ?", (account_id,)
        )


# ── Sync Log ──────────────────────────────────────────────────────────

def log_sync_start(account_id: int) -> int:
    now = datetime.now().isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO sync_log (account_id, started_at, status) VALUES (?, ?, 'running')",
            (account_id, now),
        )
        return cur.lastrowid


def log_sync_end(
    log_id: int,
    status: str,
    tx_added: int = 0,
    error: str | None = None,
) -> None:
    now = datetime.now().isoformat()
    with _connect() as conn:
        conn.execute(
            """UPDATE sync_log
               SET completed_at = ?, status = ?, tx_added = ?, error_message = ?
               WHERE id = ?""",
            (now, status, tx_added, error, log_id),
        )


def get_sync_log(account_id: int, limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM sync_log
               WHERE account_id = ?
               ORDER BY started_at DESC LIMIT ?""",
            (account_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
