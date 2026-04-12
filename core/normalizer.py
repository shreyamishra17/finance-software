"""
core/normalizer.py — Map raw transactions from any source to FIFO's internal schema.

All bank connectors produce NormalizedTx instances which are then stored
uniformly in the transactions table.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass
class NormalizedTx:
    date: str            # YYYY-MM-DD
    description: str     # raw merchant / description from bank
    merchant_clean: str  # cleaned display name
    amount: str          # absolute decimal string e.g. "42.50"
    amount_type: str     # 'credit' | 'debit'
    category: str        # FIFO category name

    def to_dict(self) -> dict:
        return {
            "date":          self.date,
            "description":   self.description,
            "merchant_clean": self.merchant_clean,
            "amount":        self.amount,
            "amount_type":   self.amount_type,
            "type":          self.amount_type,  # backward compat
            "category":      self.category,
        }


def _clean_amount(raw: str) -> str:
    """Strip currency symbols, commas, and sign — return absolute decimal string."""
    cleaned = (
        str(raw)
        .replace("$", "")
        .replace(",", "")
        .replace("+", "")
        .replace("-", "")
        .strip()
    )
    try:
        return f"{abs(float(cleaned)):.2f}"
    except ValueError:
        return cleaned or "0.00"


def _make_ref(date: str, description: str, amount: str) -> str:
    payload = f"{date}|{description}|{amount}"
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def from_scraper_record(record) -> NormalizedTx:
    """Map a bank_fetcher.TransactionRecord to NormalizedTx.
    Merchant cleaning happens in the categorization pipeline downstream."""
    amount = _clean_amount(record.amount)
    return NormalizedTx(
        date=record.date,
        description=record.description,
        merchant_clean=record.description,
        amount=amount,
        amount_type=record.amount_type,
        category="Uncategorized",
    )


def from_plaid_dict(t: dict) -> NormalizedTx:
    """Map a plaid_connector._map_transaction() result (already normalized)."""
    return NormalizedTx(
        date=t.get("date", ""),
        description=t.get("description", ""),
        merchant_clean=t.get("merchant_clean") or t.get("description", ""),
        amount=_clean_amount(t.get("amount", "0")),
        amount_type=t.get("amount_type", "debit"),
        category=t.get("category", "Uncategorized"),
    )


def from_pdf_row(row: dict) -> NormalizedTx:
    """Map a parse_bank PDF row dict to NormalizedTx."""
    direction = row.get("direction") or row.get("amount_type", "debit")
    amount = _clean_amount(str(row.get("amount", "0")))
    desc = (
        row.get("merchant_clean")
        or row.get("merchant_raw")
        or row.get("description", "")
    )
    return NormalizedTx(
        date=row.get("date", ""),
        description=desc,
        merchant_clean=desc,
        amount=amount,
        amount_type="credit" if direction in ("credit", "in") else "debit",
        category=row.get("category", "Uncategorized"),
    )
