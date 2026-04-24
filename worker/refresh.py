"""
worker/refresh.py — Background refresh logic for all linked accounts.

Called by the APScheduler job every N hours. For each account:
  - Plaid accounts: call Plaid API for new transactions
  - Scraper accounts: attempt headless session replay
  - On auth error: mark account as reauth_required (not broken yet)
  - After 3 consecutive auth errors: mark as broken
  - All runs logged to sync_log table
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.db import (
    get_accounts,
    get_account,
    store_transactions,
    update_last_synced,
    set_account_balance,
    set_account_status,
    log_sync_start,
    log_sync_end,
    get_sync_log,
)

# Number of consecutive auth failures before marking an account BROKEN
_MAX_AUTH_FAILURES = 3


def _count_recent_auth_failures(account_id: int) -> int:
    """Count consecutive 'error' entries at the top of the sync log."""
    log = get_sync_log(account_id, limit=_MAX_AUTH_FAILURES)
    count = 0
    for entry in log:
        if entry.get("status") == "error":
            count += 1
        else:
            break
    return count


def refresh_all() -> None:
    """
    Main entry point called by the scheduler (runs every 30 min).
    Only syncs accounts where auto_sync is enabled AND the per-account
    sync_interval_hours has elapsed since last_synced_at.
    """
    accounts = get_accounts()
    if not accounts:
        return

    now = datetime.now()
    for account in accounts:
        account_id = account["id"]
        status = account.get("status", "connected")

        # Skip accounts that need user action
        if status in ("pending", "otp_required", "reauth_required", "broken"):
            continue

        # Skip if auto_sync not enabled
        if not account.get("auto_sync"):
            continue

        # Check if per-account interval has elapsed
        interval_hours = int(account.get("sync_interval_hours") or 8)
        last_synced = account.get("last_synced_at")
        if last_synced:
            elapsed = (now - datetime.fromisoformat(last_synced)).total_seconds() / 3600
            if elapsed < interval_hours:
                continue  # not due yet

        print(f"[refresh] Due: {account['display_name']} (every {interval_hours}h, last synced {last_synced})")
        try:
            _refresh_account(account)
        except Exception as exc:
            print(f"[refresh] Unexpected error for account {account_id}: {exc}")


def _refresh_account(account: dict) -> None:
    account_id = account["id"]
    source = account.get("source", "scraped")
    display = account.get("display_name", f"#{account_id}")
    print(f"[refresh] Syncing {display} (source={source})…")

    log_id = log_sync_start(account_id)
    set_account_status(account_id, "refreshing")

    try:
        if source == "plaid":
            added = _refresh_plaid(account)
        else:
            added = _refresh_scraper(account)

        update_last_synced(account_id, datetime.now())
        set_account_status(account_id, "connected")
        log_sync_end(log_id, "ok", added)
        print(f"[refresh] ✓ {display}: {added} new transaction(s)")

    except _AuthError as exc:
        failures = _count_recent_auth_failures(account_id)
        new_status = "broken" if failures >= _MAX_AUTH_FAILURES else "reauth_required"
        set_account_status(account_id, new_status, str(exc))
        log_sync_end(log_id, "error", 0, str(exc))
        print(f"[refresh] ✗ {display}: auth error → {new_status} ({exc})")

    except Exception as exc:
        set_account_status(account_id, "stale", str(exc))
        log_sync_end(log_id, "error", 0, str(exc))
        print(f"[refresh] ✗ {display}: sync error → stale ({exc})")


def _refresh_plaid(account: dict) -> int:
    """Fetch new Plaid transactions since last sync. Returns count added."""
    account_id = account["id"]
    try:
        from plaid_connector import fetch_transactions, fetch_balance
    except ImportError:
        raise RuntimeError("plaid_connector not available")

    last = account.get("last_synced_at")
    since = (
        datetime.fromisoformat(last)
        if last
        else datetime.now() - timedelta(days=365)
    )
    txns = fetch_transactions(account_id, since)
    added = store_transactions(account_id, txns)

    bal = fetch_balance(account_id)
    if bal:
        set_account_balance(account_id, bal)

    return added


def _refresh_scraper(account: dict) -> int:
    """
    Attempt headless session replay for a scraper-connected account.
    Raises _AuthError if the session has expired.
    """
    account_id = account["id"]
    bank_key = account.get("bank_key", "boa")

    last = account.get("last_synced_at")
    since = (
        datetime.fromisoformat(last)
        if last
        else datetime.now() - timedelta(days=90)
    )

    try:
        from connectors.scraper import ScraperConnector
        from bank_fetcher.base_crawler import SessionExpiredError
    except ImportError as exc:
        raise RuntimeError(f"Scraper dependencies not available: {exc}")

    try:
        connector = ScraperConnector(bank_key)
        txns = connector.fetch_transactions(account_id, since)
        return store_transactions(account_id, txns)
    except SessionExpiredError as exc:
        raise _AuthError(str(exc)) from exc
    except RuntimeError as exc:
        # ScraperConnector raises RuntimeError("No valid session…") when no session stored
        if "session" in str(exc).lower() or "reconnect" in str(exc).lower():
            raise _AuthError(str(exc)) from exc
        raise


class _AuthError(Exception):
    """Signals that the refresh failed due to authentication, not a transient error."""
