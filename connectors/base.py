"""
connectors/base.py — Abstract base class for all bank connectors.

Every connector (Plaid, scraper, CSV import, etc.) implements this interface
so the rest of the system can work with any source uniformly.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime


class BaseConnector(ABC):
    @abstractmethod
    def fetch_transactions(
        self, account_id: int, since: datetime
    ) -> list[dict]:
        """Return normalized transaction dicts since the given date."""
        ...

    @abstractmethod
    def fetch_balance(self, account_id: int) -> str | None:
        """Return current balance as formatted string e.g. '$17,347.66', or None."""
        ...

    @property
    @abstractmethod
    def connector_type(self) -> str:
        """One of: 'plaid' | 'scraper' | 'pdf' | 'csv'"""
        ...
