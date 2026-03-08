#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Step1Result:
    category: str
    confidence: float
    predicted_by: str


def categorize_step_1(merchant_name: str) -> Step1Result | None:
    text = str(merchant_name or "").upper()

    # Priority 1: money transfer rails.
    if "ZELLE" in text or "VENMO" in text:
        return Step1Result(
            category="Money Transfer",
            confidence=1.0,
            predicted_by="step_1",
        )

    # Priority 2: direct deposit payroll/income-like transactions.
    if "DIRECT DEP" in text:
        return Step1Result(
            category="Income",
            confidence=1.0,
            predicted_by="step_1",
        )

    # Priority 3: Toast POS transactions for restaurants/food service.
    if "TST*" in text or "TST *" in text:
        return Step1Result(
            category="Food & Drink",
            confidence=1.0,
            predicted_by="step_1",
        )

    return None
