"""
connectors/plaid.py — Plaid connector implementing BaseConnector.

Thin wrapper around src/plaid_connector.py so the rest of the system
doesn't need to know about the Plaid SDK directly.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from connectors.base import BaseConnector


class PlaidConnector(BaseConnector):
    @property
    def connector_type(self) -> str:
        return "plaid"

    def fetch_transactions(
        self, account_id: int, since: datetime
    ) -> list[dict]:
        from plaid_connector import fetch_transactions
        return fetch_transactions(account_id, since)

    def fetch_balance(self, account_id: int) -> str | None:
        from plaid_connector import fetch_balance
        return fetch_balance(account_id)
