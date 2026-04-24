#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Optional

try:
    from parse_bank import extract_text
except ModuleNotFoundError:
    from src.parse_bank import extract_text


_MONTH_ABBRS: frozenset = frozenset({
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
})
_US_STATE_ABBRS: frozenset = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
})


def _is_noise_token(tok: str) -> bool:
    """Return True if tok is a date/location token that should not be a merchant marker."""
    if tok in _MONTH_ABBRS:
        return True
    if tok in _US_STATE_ABBRS:
        return True
    # Merged city+state with no space, e.g. DIEGOCA, JOLLACA
    if len(tok) > 4 and tok[-2:] in _US_STATE_ABBRS and tok[:-2].isalpha():
        return True
    return False


DATE_ANCHOR_RE = re.compile(
    r"^(?:"
    r"(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])(?:/\d{2})?"  # MM/DD[/YY]
    r"|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2}"  # MMM DD
    r")"
    r"(?:\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2})?"  # optional post-date
    r"\b",
    re.IGNORECASE,
)


def _load_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _detect_existing_config(lines: list[str], config_dir: Path) -> Optional[Path]:
    text = " ".join(lines).lower()
    for cfg in sorted(config_dir.glob("*_config.json")):
        data = _load_json(cfg)
        aliases = data.get("bank_aliases", [])
        if not isinstance(aliases, list):
            continue
        for alias in aliases:
            alias_str = str(alias).strip().lower()
            if alias_str and alias_str in text:
                return cfg
    return None


def _guess_bank_name(lines: list[str]) -> str:
    candidates = []
    # Capturing patterns: match group 1 is the bank name.
    capturing = [
        re.compile(r"\b([A-Z][A-Za-z&.\- ]{2,40}\bBANK(?: OF [A-Z][A-Za-z]+)?)\b"),
        re.compile(r"\b([A-Z][A-Za-z&.\- ]{2,40}\bCREDIT UNION)\b"),
        re.compile(r"\b([A-Z][A-Za-z&.\- ]{2,40}\bFINANCIAL)\b"),
    ]
    # Non-capturing patterns for known card issuers that don't use BANK/CU/FINANCIAL.
    # These return the match text directly.
    named = [
        (re.compile(r"\bCapital One\b", re.IGNORECASE), "Capital One"),
        (re.compile(r"\bAmerican Express\b", re.IGNORECASE), "American Express"),
        (re.compile(r"\bDiscover\b", re.IGNORECASE), "Discover"),
        (re.compile(r"\bChase\b", re.IGNORECASE), "Chase"),
        (re.compile(r"\bCiti(?:bank)?\b", re.IGNORECASE), "Citi"),
        (re.compile(r"\bWells Fargo\b", re.IGNORECASE), "Wells Fargo"),
    ]
    for line in lines[:120]:
        for pat in capturing:
            for m in pat.finditer(line):
                val = re.sub(r"\s+", " ", m.group(1)).strip(" -:")
                if len(val) >= 4:
                    candidates.append(val)
        for pat, name in named:
            if pat.search(line):
                candidates.append(name)
    if candidates:
        return Counter(candidates).most_common(1)[0][0]
    return "Unknown Bank"


def _slugify(value: str, fallback: str = "unknown_bank") -> str:
    out = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return out or fallback


def _config_basename(bank_name: str) -> str:
    # Prefer short, stable names like "discover_config.json".
    tokens = [t for t in re.findall(r"[A-Za-z0-9]+", bank_name.lower()) if t]
    preferred = {
        "discover": "discover",
        "chase": "chase",
        "citi": "citi",
        "citibank": "citi",
        "wells": "wells_fargo",
        "fargo": "wells_fargo",
        "amex": "amex",
        "american": "amex",
        "capital": "capital_one",
        "boa": "boa",
        "america": "boa",
    }
    for token in tokens:
        if token in preferred:
            return preferred[token]
    stop = {"bank", "of", "the", "financial", "services", "service", "credit", "union"}
    core = [t for t in tokens if t not in stop]
    if core:
        return core[0]
    if tokens:
        return tokens[0]
    return "unknown_bank"


def _extract_section_hints(lines: list[str]) -> dict[str, list[str]]:
    joined = "\n".join(lines).lower()
    sections: dict[str, list[str]] = {}

    if "deposit" in joined or "addition" in joined:
        sections["deposits"] = ["deposits", "additions"]

    if "withdrawal" in joined or "subtraction" in joined:
        # Debit-style statement (BOA checking, etc.)
        sections["withdrawals"] = ["withdrawals", "subtractions"]
    elif "trans date" in joined and "transactions" in joined:
        # Credit card format: charges live in a "Transactions" section, not "Withdrawals".
        # "trans date" (from the Trans Date / Post Date column header) is the reliable signal.
        sections["withdrawals"] = ["transactions"]
        if "payments, credits" in joined or "credits and adjustments" in joined:
            sections["deposits"] = ["payments, credits and adjustments", "payments and credits"]
        elif "payments" in joined or "credits" in joined:
            sections["deposits"] = ["payments", "credits"]

    if "service fee" in joined or "fees" in joined:
        sections["fees"] = ["service fees", "fees"]

    return sections


def _extract_stop_markers(lines: list[str]) -> list[str]:
    text = "\n".join(lines).lower()
    markers = []
    for phrase in ["account summary", "important information", "important messages", "ending balance"]:
        if phrase in text:
            markers.append(phrase)
    return markers


def _extract_table_markers(lines: list[str]) -> list[str]:
    markers = []
    candidates = [
        "date description amount",
        "date description withdrawals deposits",
        "date description",
    ]
    text = "\n".join(lines).lower()
    for c in candidates:
        if c in text:
            markers.append(c)
    return markers or ["date description amount"]


def _build_bootstrap_config(bank_name: str, lines: list[str]) -> dict:
    bank_slug = _slugify(bank_name)
    section_headers = _extract_section_hints(lines)
    section_end = _extract_stop_markers(lines)
    table_headers = _extract_table_markers(lines)

    return {
        "bank_name": bank_name,
        "bank_aliases": [bank_name.lower(), bank_slug.replace("_", " ")],
        "markers": {
            "TST*": {"type": "toast", "action": "extract_after"},
            "SQ": {"type": "square", "action": "extract_after"},
            "AMZN": {"type": "amazon", "canonical": "Amazon"},
        },
        "merchant_markers": [
            {"match": "contains", "value": "APPLE.COM/BILL", "action": "canonical", "canonical": "Apple"},
            {"match": "contains", "value": "AMZN", "action": "canonical", "canonical": "Amazon"},
            {"match": "contains", "value": "AMAZON", "action": "canonical", "canonical": "Amazon"},
            {"match": "prefix", "value": "VENMO", "action": "extract_after", "include_marker": True, "max_tokens": 2, "stop_tokens": ["VISA", "DIRECT"]},
            {"match": "token", "value": "VENMO", "action": "extract_after", "include_marker": True, "max_tokens": 2, "stop_tokens": ["VISA", "DIRECT"]},
            {"match": "prefix", "value": "TST*", "action": "extract_after"},
            {"match": "token", "value": "SQ", "action": "extract_after"},
            {"match": "prefix", "value": "SQ*", "action": "extract_after"},
            {"match": "prefix", "value": "DD*", "action": "extract_after"},
        ],
        "p2p_rules": [
            {
                "match_tokens": ["ZELLE"],
                "prefix": "Zelle payment",
                "direction_tokens": ["FROM", "TO"],
                "memo_token": "FOR",
                "stop_tokens": ["CONF#", "CONF", "ID:", "PPD", "WEB", "VISA", "DIRECT", "CONTINUED"],
                "max_name_tokens": 6,
                "min_name_tokens": 1,
            },
            {
                "match_tokens": ["VENMO", "VENMO*"],
                "prefix": "Venmo payment",
                "direction_tokens": ["FROM", "TO"],
                "memo_token": "FOR",
                "stop_tokens": ["CONF#", "CONF", "ID:", "PPD", "WEB", "VISA", "DIRECT", "CONTINUED"],
                "max_name_tokens": 6,
                "min_name_tokens": 1,
            },
        ],
        "stop_phrases": [
            "continued on the next page",
            "date description amount",
        ],
        "fee_keywords": [
            "INTERNATIONAL TRANSACTION FEE",
            "SERVICE FEES",
        ],
        "fee_labels": {
            "INTERNATIONAL TRANSACTION FEE": "International Transaction Fee",
            "SERVICE FEES": "Service Fees",
        },
        "section_headers": section_headers,
        "section_end_markers": section_end,
        "table_header_markers": table_headers,
        "boilerplate_patterns": [
            "\\bMOBILE PURCHASE\\b",
            "\\bCARD PURCHASE\\b",
            "\\bDEBIT CARD\\b",
            "\\bCHECKCARD\\b",
            "\\bDES:\\s*",
            "\\bID:\\s*",
            "\\bINDN:\\s*",
            "\\bCO ID:\\s*",
            "\\bPPD\\b",
            "\\bWEB\\b",
        ],
        "header_hints": [
            "statement",
            "account number",
            "account #",
            "balance",
            "payment",
            "page ",
        ],
        "ignorable_tail_patterns": [
            "(?i)ID:?.*",
            "(?i)CO",
            "(?i)PPD",
            "(?i)WEB",
            "(?i)DES:?.*",
            "(?i)INDN:?.*",
            "(?i)continued",
            "#?[0-9]{6,}",
        ],
    }


def _build_suggestion_report(lines: list[str]) -> dict:
    marker_counter: Counter[str] = Counter()
    examples: list[str] = []
    for line in lines:
        if not DATE_ANCHOR_RE.match(line):
            continue
        examples.append(line)
        upper = line.upper()
        tokens = re.findall(r"[A-Z][A-Z0-9/*.-]{1,20}", upper)
        for tok in tokens[:8]:
            if tok in {"MOBILE", "PURCHASE", "CHECKCARD", "DATE", "DESCRIPTION", "AMOUNT"}:
                continue
            if _is_noise_token(tok):
                continue
            marker_counter[tok] += 1

    return {
        "suggested_merchant_markers": [
            {"token": token, "count": count}
            for token, count in marker_counter.most_common(30)
        ],
        "sample_transaction_lines": examples[:30],
    }


def bootstrap_config_if_unknown(
    pdf_path: Path,
    config_dir: Optional[Path] = None,
    force: bool = False,
) -> dict:
    config_dir = (config_dir or (Path(__file__).resolve().parent.parent / "config" / "banks")).expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)

    lines = extract_text(pdf_path)
    if not lines:
        return {"created": False, "reason": "no_text"}

    existing = _detect_existing_config(lines, config_dir)
    if existing:
        return {"created": False, "reason": "existing_config", "config_path": str(existing)}

    bank_name = _guess_bank_name(lines)
    slug = _slugify(bank_name)
    base = _config_basename(bank_name)
    config_path = config_dir / f"{base}_config.json"
    report_path = config_dir / f"{base}_bootstrap_report.json"

    # Avoid overwrite if short base is already taken by another bank family.
    if config_path.exists() and not force:
        existing = _load_json(config_path)
        existing_name = str(existing.get("bank_name") or "").strip().lower()
        if existing_name and existing_name != bank_name.lower():
            config_path = config_dir / f"{slug}_config.json"
            report_path = config_dir / f"{slug}_bootstrap_report.json"

    if config_path.exists() and not force:
        return {"created": False, "reason": "config_exists", "config_path": str(config_path)}

    cfg = _build_bootstrap_config(bank_name, lines)
    report = _build_suggestion_report(lines)

    with config_path.open("w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")

    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
        fh.write("\n")

    return {
        "created": True,
        "bank_name": bank_name,
        "config_path": str(config_path),
        "report_path": str(report_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap a new bank config from a statement PDF.")
    parser.add_argument("pdf", type=Path, help="Path to statement PDF")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "config" / "banks",
        help="Config directory (default: ./config/banks)",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing target config if present")
    args = parser.parse_args()

    pdf_path = args.pdf.expanduser()
    config_dir = args.config_dir.expanduser()

    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}")
        return 1

    config_dir.mkdir(parents=True, exist_ok=True)
    result = bootstrap_config_if_unknown(pdf_path, config_dir=config_dir, force=args.force)
    if result.get("reason") == "no_text":
        print("No text extracted from PDF.")
        return 1
    if result.get("reason") == "existing_config":
        print(f"Existing config already matches this statement: {result['config_path']}")
        return 0
    if result.get("reason") == "config_exists":
        print(f"Config already exists: {result['config_path']}")
        print("Use --force to overwrite.")
        return 1
    if result.get("created"):
        print(f"Created config: {result['config_path']}")
        print(f"Created report: {result['report_path']}")
        print("Next step: tune merchant_markers and section markers using the report samples.")
        return 0
    print("No action taken.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
