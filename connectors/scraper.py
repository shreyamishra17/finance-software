"""
connectors/scraper.py — Playwright-based scraper connector.

Two modes:
  1. headless  — reuses an encrypted browser session stored in account_sessions.
                 Raises SessionExpiredError if the session is stale.
  2. interactive — opens a real browser window, handles OTP, saves session.

Session storage
---------------
After a successful interactive login, Playwright's storage_state JSON is
encrypted with Fernet (core.encryption) and persisted in the account_sessions
table. The plaintext file is deleted immediately after encryption so credentials
never sit on disk unprotected.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from connectors.base import BaseConnector
from core.db import (
    get_account,
    load_session,
    save_session,
    delete_session,
    set_account_status,
    set_needs_login,
)
from core.encryption import encrypt, decrypt
from core.normalizer import from_scraper_record

DATA_DIR = ROOT / "data"
SESSIONS_DIR = DATA_DIR / "sessions"

try:
    from bank_fetcher.base_crawler import BankCrawler, SessionExpiredError
    from bank_fetcher import _load_fetcher_config
    _SCRAPER_AVAILABLE = True
except ImportError:
    _SCRAPER_AVAILABLE = False
    BankCrawler = None
    SessionExpiredError = RuntimeError


class ScraperConnector(BaseConnector):
    def __init__(self, bank_key: str) -> None:
        self._bank_key = bank_key

    @property
    def connector_type(self) -> str:
        return "scraper"

    # ── Public interface ──────────────────────────────────────────────

    def fetch_transactions(
        self, account_id: int, since: datetime
    ) -> list[dict]:
        """
        Attempt a headless session replay.
        Raises SessionExpiredError (mapped to reauth_required) if the session
        is stale or missing.
        """
        if not _SCRAPER_AVAILABLE:
            raise RuntimeError("bank_fetcher package is not available")

        session_row = load_session(account_id)
        if not (session_row and session_row.get("session_data_enc")):
            self._mark_reauth(account_id, "No session stored — reconnect required")
            raise RuntimeError(
                "No valid session. Use the reconnect flow to log in interactively."
            )

        # Decrypt session to a temp file for Playwright
        session_path = SESSIONS_DIR / f"{account_id}.json"
        try:
            session_json = decrypt(session_row["session_data_enc"])
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text(session_json)

            cfg = _load_fetcher_config(self._bank_key)
            crawler = BankCrawler(cfg, DATA_DIR / "raw")
            records = crawler.run_scrape_headless(
                since_date=since,
                session_path=session_path,
                account_url=session_row.get("session_url"),
            )
            return [from_scraper_record(r).to_dict() for r in records]

        except SessionExpiredError:
            delete_session(account_id)
            self._mark_reauth(account_id, "Browser session expired — reconnect required")
            raise

        finally:
            # Never leave decrypted session on disk
            session_path.unlink(missing_ok=True)

    def fetch_balance(self, account_id: int) -> str | None:
        # Balance is captured from transaction rows during scrape, not separately
        return None

    def scrape_interactive(
        self,
        account_id: int,
        since: datetime,
    ) -> list[dict]:
        """
        Full interactive login + scrape. Opens a visible browser window.
        Saves the resulting session (encrypted) for future headless use.
        """
        if not _SCRAPER_AVAILABLE:
            raise RuntimeError("bank_fetcher package is not available")

        cfg = _load_fetcher_config(self._bank_key)
        crawler = BankCrawler(cfg, DATA_DIR / "raw")

        session_path = SESSIONS_DIR / f"{account_id}.json"
        session_path.parent.mkdir(parents=True, exist_ok=True)

        records, transactions_url = crawler.run_scrape(
            since_date=since,
            session_save_path=session_path,
        )

        # Encrypt and persist session; delete plaintext immediately
        if session_path.exists():
            enc = encrypt(session_path.read_text())
            expires_at = (datetime.now() + timedelta(days=30)).isoformat()
            save_session(account_id, enc, transactions_url, expires_at)
            session_path.unlink(missing_ok=True)

        return [from_scraper_record(r).to_dict() for r in records]

    # ── Helpers ───────────────────────────────────────────────────────

    def _mark_reauth(self, account_id: int, reason: str) -> None:
        set_account_status(account_id, "reauth_required", reason)
        set_needs_login(account_id, True)
