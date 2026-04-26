"""
db.py — database backend router.

  Local dev  → SQLite  (fast, zero-config, data stays on your machine)
  Cloud Run  → Firestore (persistent across restarts and redeployments)

Detection is automatic:
  - Cloud Run always sets the K_SERVICE env var → Firestore is used.
  - Anything else (localhost, CI) → SQLite.
  - Override: set USE_FIRESTORE=1 to force Firestore locally
    (useful if you want to inspect production data from your machine).
"""
from __future__ import annotations
import os as _os

_USE_FIRESTORE = bool(_os.environ.get("K_SERVICE") or _os.environ.get("USE_FIRESTORE"))

if _USE_FIRESTORE:
    from db_firestore import (  # noqa: F401
        init_db,
        add_account,
        get_accounts,
        get_account,
        update_last_synced,
        delete_account,
        set_auto_sync,
        set_needs_login,
        set_last_url,
        set_account_balance,
        set_plaid_item_id,
        store_transactions,
        update_transaction_categories,
        get_transactions,
        get_transactions_for_account_raw,
        get_uncategorized_transactions,
        update_transaction_by_id,
        _normalize_date,
    )
else:
    from db_sqlite import (  # noqa: F401
        init_db,
        add_account,
        get_accounts,
        get_account,
        update_last_synced,
        delete_account,
        set_auto_sync,
        set_needs_login,
        set_last_url,
        set_account_balance,
        set_plaid_item_id,
        store_transactions,
        update_transaction_categories,
        get_transactions,
        get_transactions_for_account_raw,
        get_uncategorized_transactions,
        update_transaction_by_id,
        _normalize_date,
    )
