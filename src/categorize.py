#!/usr/bin/env python3
from __future__ import annotations

import json
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "category_rules.json"


@dataclass
class CategoryResult:
    category: str
    confidence: float
    matched_rule: Optional[str] = None
    matched_keyword: Optional[str] = None
    layer: str = "default"


def _load_rules(path: Optional[Path]) -> dict:
    rules_path = path or DEFAULT_RULES_PATH
    try:
        with rules_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {
            "default_category": "Uncategorized",
            "default_confidence": 0.2,
            "high_confidence": [],
            "merchant_rules": [],
        }
    return data


def _normalize(text: str) -> str:
    return " ".join(text.upper().split())


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    if keyword.lower().startswith("re:"):
        return re.compile(keyword[3:], re.IGNORECASE)
    escaped = re.escape(keyword)
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


def _find_keyword_match(haystack: str, keywords: Iterable[str]) -> Optional[str]:
    for kw in keywords:
        kw_norm = _normalize(str(kw))
        if not kw_norm:
            continue
        if _keyword_pattern(kw_norm).search(haystack):
            return kw_norm
    return None


def categorize_transaction(txn: dict, rules_path: Optional[Path] = None) -> CategoryResult:
    rules = _load_rules(rules_path)

    default_category = str(rules.get("default_category", "Uncategorized"))
    default_confidence = float(rules.get("default_confidence", 0.2))

    raw = str(txn.get("raw_description", ""))
    merchant = str(txn.get("merchant_clean", ""))
    haystack = _normalize(" ".join([merchant, raw]))

    for rule in rules.get("high_confidence", []):
        keywords = rule.get("keywords", [])
        matched = _find_keyword_match(haystack, keywords)
        if matched:
            return CategoryResult(
                category=str(rule.get("category", default_category)),
                confidence=float(rule.get("confidence", 0.9)),
                matched_rule=str(rule.get("category", default_category)),
                matched_keyword=matched,
                layer="high_confidence",
            )

    for rule in rules.get("merchant_rules", []):
        keywords = rule.get("keywords", [])
        matched = _find_keyword_match(_normalize(merchant), keywords) or _find_keyword_match(haystack, keywords)
        if matched:
            return CategoryResult(
                category=str(rule.get("category", default_category)),
                confidence=float(rule.get("confidence", 0.6)),
                matched_rule=str(rule.get("category", default_category)),
                matched_keyword=matched,
                layer="merchant_rules",
            )

    return CategoryResult(
        category=default_category,
        confidence=default_confidence,
        matched_rule=default_category,
        matched_keyword=None,
        layer="default",
    )


def categorize_transactions(
    transactions: Iterable[dict],
    rules_path: Optional[Path] = None,
) -> list[dict]:
    categorized = []
    for txn in transactions:
        result = categorize_transaction(txn, rules_path)
        updated = dict(txn)
        updated["category"] = result.category
        updated["confidence"] = result.confidence
        updated["matched_rule"] = result.matched_rule
        updated["matched_keyword"] = result.matched_keyword
        updated["category_layer"] = result.layer
        categorized.append(updated)
    return categorized


if __name__ == "__main__":
    # Minimal smoke test when run directly.
    sample = {
        "raw_description": "MOBILE PURCHASE WHOLEFDS LAJ 100 LA JOLLA CA",
        "merchant_clean": "WHOLEFDS LAJ",
        "amount": "-45.00",
    }
    print(categorize_transaction(sample))
