#!/usr/bin/env python3
from __future__ import annotations

import json
import csv
import os
import hashlib
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional


DATE_ANCHOR_RE = re.compile(
    r"^(?P<date>(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(?:/\d{2})?)\b"
)
AMOUNT_RE = re.compile(r"(?P<amount>\(?-?\$?\d[\d,]*\.\d{2}\)?)")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("FINELY_DATA_ROOT", str(PROJECT_ROOT / "my_finance")))
RAW_STATEMENTS_DIR = Path(os.environ.get("FINELY_RAW_DIR", str(DATA_ROOT / "raw")))
PROCESSED_DIR = Path(os.environ.get("FINELY_PROCESSED_DIR", str(DATA_ROOT / "processed")))


class ParserConfig:
    COMMON_CONFIG_PATH = PROJECT_ROOT / "config" / "common_config.json"
    # These regex patterns never change — kep
    #  as class attributes so they're
    # accessible both as ParserConfig.X (backwards compat) and _cfg().X (instance lookup).
    PHONE_RE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")
    MMDD_RE = re.compile(r"\b(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\b")
    MM_DD_RE = re.compile(r"\b(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])\b")
    LONG_ID_RE = re.compile(r"\b[0-9]{8,}\b")

    def __init__(self) -> None:
        self.CONFIG_PATH: Optional[Path] = None
        self.BOILERPLATE_PATTERNS: List[str] = []
        self.STATE_ABBRS: set[str] = set()
        self.MARKERS: dict = {}
        self.LOCATION_WORDS: set[str] = set()
        self.LOCATION_PREFIX_WORDS: set[str] = set()
        self.LOCATION_PHRASES: List[str] = []
        self.STOP_PHRASES: List[str] = []
        self.FEE_KEYWORDS: List[str] = []
        self.FEE_LABELS: dict = {}
        self.SECTION_HEADERS: dict = {}
        self.SECTION_END_MARKERS: List[str] = []
        self.TABLE_HEADER_MARKERS: List[str] = []
        self.MERCHANT_MARKERS: List[dict] = []
        self.P2P_RULES: List[dict] = []
        self.BANK_NAME: Optional[str] = None
        self.BANK_ALIASES: List[str] = []
        self.IGNORABLE_TAIL_PATTERNS: List[str] = []
        self.METADATA_LABELS: List[str] = ["DES", "ID", "INDN", "CO ID", "PPD", "WEB", "INFO", "PMT"]
        self.PREFERRED_MERCHANT_LABELS: List[str] = ["DES"]
        self.SUMMARY_ALIASES: dict[str, list[str]] = {
            "beginning_balance": ["beginning balance", "opening balance", "previous balance", "starting balance"],
            "deposits_additions": ["deposits", "credits", "payments and credits", "money in", "total credits"],
            "withdrawals_subtractions": ["withdrawals", "debits", "purchases", "money out", "total debits"],
            "checks": ["checks", "check payments"],
            "service_fees": ["service fees", "fees charged", "finance charges", "charges and fees"],
            "ending_balance": ["ending balance", "new balance", "closing balance", "current balance"],
        }
        self.TRAILING_MERCHANT_DESCRIPTORS: List[str] = []
        self.MERCHANT_PROCESSOR_PREFIXES: List[str] = ["SP", "SQ", "TST*", "DD", "PAYPAL", "PP*"]
        self.MERCHANT_TAIL_JUNK_PATTERNS: List[str] = [
            r"^\d{2,4}-$",
            r"^[A-Z]\d{4,}$",
            r"^\d+[A-Z]+$",
            r"^[A-Z]+\d+$",
        ]
        self.MERCHANT_SHORT_TOKEN_KEEP: set[str] = {
            "BB",
            "JR",
            "SR",
            "US",
            "UK",
            "NY",
            "LA",
            "SF",
        }
        self.MERCHANT_CONNECTOR_TOKENS: set[str] = {"&", "AND", "THE", "OF", "IN", "AT", "TO", "FOR"}
        self.MERCHANT_URL_TLD_STRIP: List[str] = ["com", "org", "net", "ca", "io"]
        self.NON_TRANSACTION_PATTERNS: List[str] = []
        self.header_hints: List[str] = []
        self.PAYMENT_CHANNEL_RULES: List[tuple[str, str]] = [
            ("EXT TRNSFR", "External Transfer"),
            ("EXT TRNSF", "External Transfer"),
            ("EXTERNAL TRANSFER", "External Transfer"),
            ("E-PAYMENT", "E-Payment"),
            ("ZELLE", "Zelle"),
            ("VENMO", "Venmo"),
            ("DIRECT DEP", "Direct Dep"),
            ("CHECKCARD", "Checkcard"),
            ("MOBILE PURCHASE", "Mobile Purchase"),
            ("CARD PURCHASE", "Card Purchase"),
            ("PURCHASE", "Purchase"),
            ("ACH", "ACH"),
            ("WIRE", "Wire"),
        ]
        self.TRANSACTION_CODE_PATTERNS: List[str] = [
            r"\bSQ\b",
            r"\bTST\*?\b",
            r"\bDES:?\b",
            r"\bID:?\b",
            r"\bACH\b",
        ]
        self.STATEMENT_TYPE_CREDIT_SIGNALS: List[str] = [
            "minimum payment due", "credit limit", "available credit",
            "new balance", "payments and credits", "purchases",
            "cash advance", "balance transfer",
        ]
        self.STATEMENT_TYPE_DEBIT_SIGNALS: List[str] = [
            "beginning balance", "ending balance", "deposits",
            "withdrawals", "checks", "service fees",
        ]
        self.SUMMARY_IGNORE_MARKERS: List[str] = [
            "trans date", "merchantcategory", "importantinformation",
            "cashback bonus", "redeemedthisperiod", "warning",
            "minimum payment warning", "midnight eastern",
            "account made after", "cardmember agreement",
            "directpayautomaticpayment",
            "your next directpay automatic payment", "willbeon",
        ]
        self.SUMMARY_SIGNAL_TERMS: set[str] = {
            "balance", "payment", "payments", "credit", "credits",
            "purchase", "purchases", "debit", "debits", "fee", "fees",
            "interest", "checks", "deposit", "deposits",
            "withdrawal", "withdrawals", "due", "minimum", "total",
        }
        self.SUMMARY_CANONICAL_LABELS: dict[str, str] = {
            "beginning_balance": "Previous Balance",
            "deposits_additions": "Payments and Credits",
            "withdrawals_subtractions": "Purchases",
            "checks": "Checks",
            "service_fees": "Fees Charged",
            "ending_balance": "New Balance",
        }

    def _apply_config_data(self, data: dict) -> None:
        """Apply fields from a parsed JSON config dict onto this instance."""
        markers = data.get("markers", {})
        if isinstance(markers, dict):
            self.MARKERS = {str(k): v for k, v in markers.items()}

        stop_phrases = data.get("stop_phrases", [])
        if isinstance(stop_phrases, list):
            self.STOP_PHRASES = [str(x).lower() for x in stop_phrases if str(x).strip()]

        fee_keywords = data.get("fee_keywords", [])
        if isinstance(fee_keywords, list):
            self.FEE_KEYWORDS = [str(x).upper() for x in fee_keywords if str(x).strip()]

        fee_labels = data.get("fee_labels", {})
        if isinstance(fee_labels, dict):
            self.FEE_LABELS = {str(k).upper(): str(v) for k, v in fee_labels.items() if str(k).strip()}

        boilerplate = data.get("boilerplate_patterns", [])
        if isinstance(boilerplate, list):
            self.BOILERPLATE_PATTERNS = [str(x) for x in boilerplate if str(x).strip()]

        state_abbrs = data.get("state_abbrs", [])
        if isinstance(state_abbrs, list):
            parsed = {str(x).upper() for x in state_abbrs if str(x).strip()}
            if parsed:
                self.STATE_ABBRS = parsed

        locations = data.get("locations", [])
        if isinstance(locations, list):
            normalized = [str(x).strip() for x in locations if str(x).strip()]
            if normalized:
                self.LOCATION_PHRASES = normalized
                words: set[str] = set()
                prefix_words: set[str] = set()
                for phrase in normalized:
                    parts = phrase.lower().split()
                    for part in parts:
                        words.add(part)
                    if len(parts) > 1:
                        prefix_words.update(parts[:-1])
                self.LOCATION_WORDS = words
                self.LOCATION_PREFIX_WORDS = prefix_words

        header_hints = data.get("header_hints", [])
        if isinstance(header_hints, list):
            self.header_hints = [str(x).lower() for x in header_hints if str(x).strip()]

        section_headers = data.get("section_headers", {})
        if isinstance(section_headers, dict):
            self.SECTION_HEADERS = {
                str(k): [str(x).lower() for x in v if str(x).strip()]
                for k, v in section_headers.items()
                if isinstance(v, list)
            }

        section_end = data.get("section_end_markers", [])
        if isinstance(section_end, list):
            self.SECTION_END_MARKERS = [str(x).lower() for x in section_end if str(x).strip()]

        table_headers = data.get("table_header_markers", [])
        if isinstance(table_headers, list):
            self.TABLE_HEADER_MARKERS = [str(x).lower() for x in table_headers if str(x).strip()]

        merchant_markers = data.get("merchant_markers", [])
        if isinstance(merchant_markers, list):
            parsed_mm = [m for m in merchant_markers if isinstance(m, dict)]
            if parsed_mm:
                self.MERCHANT_MARKERS.extend(parsed_mm)

        p2p_rules = data.get("p2p_rules", [])
        if isinstance(p2p_rules, list):
            parsed_p2p = [r for r in p2p_rules if isinstance(r, dict)]
            if parsed_p2p:
                self.P2P_RULES.extend(parsed_p2p)

        bank_name = data.get("bank_name")
        if isinstance(bank_name, str) and bank_name.strip():
            self.BANK_NAME = bank_name.strip()

        bank_aliases = data.get("bank_aliases", [])
        if isinstance(bank_aliases, list):
            self.BANK_ALIASES = [str(x).lower() for x in bank_aliases if str(x).strip()]

        ignorable_tail = data.get("ignorable_tail_patterns", [])
        if isinstance(ignorable_tail, list):
            self.IGNORABLE_TAIL_PATTERNS = [str(x) for x in ignorable_tail if str(x).strip()]

        metadata_labels = data.get("metadata_labels", [])
        if isinstance(metadata_labels, list):
            parsed_ml = [str(x).strip() for x in metadata_labels if str(x).strip()]
            if parsed_ml:
                self.METADATA_LABELS = parsed_ml

        preferred_labels = data.get("preferred_merchant_labels", [])
        if isinstance(preferred_labels, list):
            parsed_pl = [str(x).strip() for x in preferred_labels if str(x).strip()]
            if parsed_pl:
                self.PREFERRED_MERCHANT_LABELS = parsed_pl

        summary_aliases = data.get("summary_aliases", {})
        if isinstance(summary_aliases, dict):
            parsed_sa: dict[str, list[str]] = {}
            for key, values in summary_aliases.items():
                if not isinstance(values, list):
                    continue
                vals = [str(v).lower().strip() for v in values if str(v).strip()]
                if vals:
                    parsed_sa[str(key)] = vals
            if parsed_sa:
                self.SUMMARY_ALIASES = parsed_sa

        trailing_desc = data.get("trailing_merchant_descriptors", [])
        if isinstance(trailing_desc, list):
            parsed_td = [str(x).strip().upper() for x in trailing_desc if str(x).strip()]
            if parsed_td:
                self.TRAILING_MERCHANT_DESCRIPTORS = parsed_td

        processor_prefixes = data.get("merchant_processor_prefixes", [])
        if isinstance(processor_prefixes, list):
            parsed_pp = [str(x).strip().upper() for x in processor_prefixes if str(x).strip()]
            if parsed_pp:
                self.MERCHANT_PROCESSOR_PREFIXES = parsed_pp

        tail_junk_patterns = data.get("merchant_tail_junk_patterns", [])
        if isinstance(tail_junk_patterns, list):
            parsed_tj = [str(x).strip() for x in tail_junk_patterns if str(x).strip()]
            if parsed_tj:
                self.MERCHANT_TAIL_JUNK_PATTERNS = parsed_tj

        short_keep = data.get("merchant_short_token_keep", [])
        if isinstance(short_keep, list):
            parsed_sk = {str(x).strip().upper() for x in short_keep if str(x).strip()}
            if parsed_sk:
                self.MERCHANT_SHORT_TOKEN_KEEP = parsed_sk

        connector_tokens = data.get("merchant_connector_tokens", [])
        if isinstance(connector_tokens, list):
            parsed_ct = {str(x).strip().upper() for x in connector_tokens if str(x).strip()}
            if parsed_ct:
                self.MERCHANT_CONNECTOR_TOKENS = parsed_ct

        url_tld_strip = data.get("merchant_url_tld_strip", [])
        if isinstance(url_tld_strip, list):
            parsed_tld = [str(x).strip().lower() for x in url_tld_strip if str(x).strip()]
            if parsed_tld:
                self.MERCHANT_URL_TLD_STRIP = parsed_tld

        non_txn = data.get("non_transaction_patterns", [])
        if isinstance(non_txn, list):
            parsed_nt = [str(x).strip() for x in non_txn if str(x).strip()]
            if parsed_nt:
                self.NON_TRANSACTION_PATTERNS = parsed_nt

        payment_channel_rules = data.get("payment_channel_rules", [])
        if isinstance(payment_channel_rules, list):
            parsed_pcr = [
                (str(r[0]).upper(), str(r[1]))
                for r in payment_channel_rules
                if isinstance(r, (list, tuple)) and len(r) == 2
            ]
            if parsed_pcr:
                self.PAYMENT_CHANNEL_RULES = parsed_pcr

        transaction_code_patterns = data.get("transaction_code_patterns", [])
        if isinstance(transaction_code_patterns, list):
            parsed_tcp = [str(x) for x in transaction_code_patterns if str(x).strip()]
            if parsed_tcp:
                self.TRANSACTION_CODE_PATTERNS = parsed_tcp

        credit_signals = data.get("statement_type_credit_signals", [])
        if isinstance(credit_signals, list):
            parsed_cs = [str(x).lower().strip() for x in credit_signals if str(x).strip()]
            if parsed_cs:
                self.STATEMENT_TYPE_CREDIT_SIGNALS = parsed_cs

        debit_signals = data.get("statement_type_debit_signals", [])
        if isinstance(debit_signals, list):
            parsed_ds = [str(x).lower().strip() for x in debit_signals if str(x).strip()]
            if parsed_ds:
                self.STATEMENT_TYPE_DEBIT_SIGNALS = parsed_ds

        summary_ignore = data.get("summary_ignore_markers", [])
        if isinstance(summary_ignore, list):
            parsed_si = [str(x).lower().strip() for x in summary_ignore if str(x).strip()]
            if parsed_si:
                self.SUMMARY_IGNORE_MARKERS = parsed_si

        summary_signal_terms = data.get("summary_signal_terms", [])
        if isinstance(summary_signal_terms, list):
            parsed_sst = {str(x).lower().strip() for x in summary_signal_terms if str(x).strip()}
            if parsed_sst:
                self.SUMMARY_SIGNAL_TERMS = parsed_sst

        summary_canonical = data.get("summary_canonical_labels", {})
        if isinstance(summary_canonical, dict):
            parsed_scl = {str(k): str(v) for k, v in summary_canonical.items() if str(k).strip() and str(v).strip()}
            if parsed_scl:
                self.SUMMARY_CANONICAL_LABELS = parsed_scl

    def _load_common_defaults(self) -> None:
        path = self.COMMON_CONFIG_PATH
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        self._apply_config_data(data)

    def load_from_json(self, config_path: Optional[Path] = None) -> None:
        self.__init__()
        self._load_common_defaults()
        if config_path is not None:
            self.CONFIG_PATH = config_path
        if self.CONFIG_PATH is None or not self.CONFIG_PATH.exists():
            return
        try:
            with self.CONFIG_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        self._apply_config_data(data)


class MerchantNormalizationConfig:
    CONFIG_PATH = PROJECT_ROOT / "config" / "common_config.json"

    def __init__(self) -> None:
        self.TOKEN_REPLACEMENTS: dict[str, str] = {}
        self.ALIASES: dict[str, str] = {}
        self.CONTAINS_ALIASES: dict[str, str] = {}
        self.STRIP_SUFFIX_TOKENS: set[str] = set()

    def load_from_json(self, config_path: Optional[Path] = None) -> None:
        path = config_path or self.CONFIG_PATH
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return

        token_replacements = data.get("token_replacements", {})
        if isinstance(token_replacements, dict):
            self.TOKEN_REPLACEMENTS = {
                str(k).upper().strip(): str(v).strip()
                for k, v in token_replacements.items()
                if str(k).strip() and str(v).strip()
            }

        aliases = data.get("aliases", {})
        if isinstance(aliases, dict):
            self.ALIASES = {
                str(k).upper().strip(): str(v).strip()
                for k, v in aliases.items()
                if str(k).strip() and str(v).strip()
            }

        contains_aliases = data.get("contains_aliases", {})
        if isinstance(contains_aliases, dict):
            self.CONTAINS_ALIASES = {
                str(k).upper().strip(): str(v).strip()
                for k, v in contains_aliases.items()
                if str(k).strip() and str(v).strip()
            }

        strip_suffix_tokens = data.get("strip_suffix_tokens", [])
        if isinstance(strip_suffix_tokens, list):
            self.STRIP_SUFFIX_TOKENS = {str(x).upper().strip() for x in strip_suffix_tokens if str(x).strip()}


# ---------------------------------------------------------------------------
# Thread-local storage — each thread gets its own isolated config instances
# and parse stats, so concurrent PDF uploads never interfere with each other.
# ---------------------------------------------------------------------------
_thread_local = threading.local()


def _cfg() -> ParserConfig:
    """Return the ParserConfig instance for the current thread."""
    if not hasattr(_thread_local, "parser_config"):
        _thread_local.parser_config = ParserConfig()
    return _thread_local.parser_config


def _merch_cfg() -> MerchantNormalizationConfig:
    """Return the MerchantNormalizationConfig instance for the current thread."""
    if not hasattr(_thread_local, "merch_config"):
        _thread_local.merch_config = MerchantNormalizationConfig()
    return _thread_local.merch_config


def _last_parse_stats() -> Optional["ParseStats"]:
    """Return the ParseStats recorded by the most recent parse on this thread."""
    return getattr(_thread_local, "last_parse_stats", None)


def _set_last_parse_stats(stats: "ParseStats") -> None:
    """Store parse stats for the current thread."""
    _thread_local.last_parse_stats = stats


def _strip_token_punct(token: str) -> str:
    # Strip common punctuation, including asterisks used in statement markers.
    return token.strip(" ,;:*()[]{}<>")


def _is_state_abbr(token: str) -> bool:
    return token.isalpha() and token.isupper() and token in _cfg().STATE_ABBRS


def safe_cleanup(raw: str) -> str:
    text = raw.strip()

    # Remove boilerplate phrases conservatively.
    for pattern in _cfg().BOILERPLATE_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)

    # Remove phone numbers, short MM/DD or MMDD date tokens, and long numeric IDs.
    text = ParserConfig.PHONE_RE.sub(" ", text)
    text = ParserConfig.MM_DD_RE.sub(" ", text)
    text = ParserConfig.MMDD_RE.sub(" ", text)
    text = ParserConfig.LONG_ID_RE.sub(" ", text)

    # Tokenize for numeric/ID cleanup.
    tokens: List[str] = []
    for token in text.split():
        token = _strip_token_punct(token)
        if not token:
            continue

        digits = sum(ch.isdigit() for ch in token)
        if digits >= 8:
            continue
        if len(token) >= 10 and digits / len(token) >= 0.6:
            continue
        tokens.append(token)

    cleaned = " ".join(tokens)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -.,;:/")
    return cleaned


def _truncate_at_garbage(text: str) -> str:
    lower = text.lower()
    cut = None
    for phrase in _cfg().STOP_PHRASES:
        idx = lower.find(phrase)
        if idx != -1:
            cut = idx if cut is None else min(cut, idx)
    return text[:cut].strip() if cut is not None else text



def _tokenize_basic(text: str) -> List[str]:
    return [t for t in re.split(r"\s+", text.strip()) if t]


def _is_stop_token(token: str) -> bool:
    if not token:
        return True
    if token.isdigit():
        return True
    if _is_state_abbr(token.upper()):
        return True
    if re.fullmatch(r"[0-9\-./]+", token):
        return True
    return False


def _stage_b_extract(raw: str) -> str:
    text = raw.strip()
    upper = text.upper()
    tokens = _tokenize_basic(text)
    upper_tokens = [t.upper() for t in tokens]

    p2p_result = _extract_p2p(text, upper_tokens)
    if p2p_result:
        return p2p_result

    for rule in _cfg().MERCHANT_MARKERS:
        match = str(rule.get("match", "")).lower()
        value = str(rule.get("value", "")).upper()
        action = str(rule.get("action", "")).lower()
        canonical = str(rule.get("canonical", "")).strip()
        max_tokens = int(rule.get("max_tokens", 0) or 0)
        stop_tokens = {str(x).upper() for x in rule.get("stop_tokens", [])}
        include_marker = bool(rule.get("include_marker", False))

        if match == "contains" and value and value in upper:
            if action == "canonical" and canonical:
                return canonical
            if action == "extract_after":
                idx = upper.find(value)
                remainder = text[idx + len(value) :].strip()
                parts = []
                for token in remainder.split():
                    up = token.upper()
                    if up in stop_tokens or _is_stop_token(up):
                        break
                    parts.append(token)
                    if max_tokens and len(parts) >= max_tokens:
                        break
                extracted = " ".join(parts).strip() or text
                if include_marker:
                    return f"{value} {extracted}".strip()
                return extracted

        if match in {"token", "prefix"} and value:
            for i, token in enumerate(upper_tokens):
                if (match == "token" and token == value) or (match == "prefix" and token.startswith(value)):
                    if action == "canonical" and canonical:
                        return canonical
                    if action == "extract_after":
                        attached = ""
                        if match == "prefix" and len(token) > len(value):
                            attached = token[len(value) :]
                        parts = [attached] if attached else []
                        for nxt in tokens[i + 1 :]:
                            up = nxt.upper()
                            if up in stop_tokens or _is_stop_token(up):
                                break
                            parts.append(nxt)
                            if max_tokens and len(parts) >= max_tokens:
                                break
                        extracted = " ".join([p for p in parts if p]).strip() or text
                        if include_marker:
                            return f"{value} {extracted}".strip()
                        return extracted

    return text


def _extract_p2p(text: str, upper_tokens: List[str]) -> Optional[str]:
    for rule in _cfg().P2P_RULES:
        match_tokens = [str(x).upper() for x in rule.get("match_tokens", []) if str(x).strip()]
        if not match_tokens:
            continue
        if not any(tok in upper_tokens for tok in match_tokens):
            continue

        direction_tokens = [str(x).upper() for x in rule.get("direction_tokens", []) if str(x).strip()]
        memo_token = str(rule.get("memo_token", "")).upper().strip()
        stop_tokens = {str(x).upper() for x in rule.get("stop_tokens", []) if str(x).strip()}
        max_name_tokens = int(rule.get("max_name_tokens", 5) or 5)
        prefix = str(rule.get("prefix", "P2P payment")).strip()
        min_name_tokens = int(rule.get("min_name_tokens", 1) or 1)

        direction_idx = -1
        direction = None
        for idx, token in enumerate(upper_tokens):
            if token in direction_tokens:
                direction_idx = idx
                direction = token.lower()
                break
        if direction_idx == -1 or direction is None:
            continue

        def is_stop(token: str) -> bool:
            upper_tok = token.upper()
            if upper_tok in stop_tokens:
                return True
            if any(upper_tok.startswith(st) for st in stop_tokens):
                return True
            if _is_stop_token(upper_tok):
                return True
            return False

        raw_tokens = _tokenize_basic(text)
        name_parts: List[str] = []
        memo_parts: List[str] = []

        i = direction_idx + 1
        while i < len(raw_tokens):
            token = raw_tokens[i]
            upper_tok = upper_tokens[i]
            if memo_token and upper_tok == memo_token:
                break
            if is_stop(token):
                break
            name_parts.append(token)
            if len(name_parts) >= max_name_tokens:
                break
            i += 1

        if memo_token:
            memo_idx = -1
            for j in range(direction_idx + 1, len(upper_tokens)):
                if upper_tokens[j] == memo_token:
                    memo_idx = j
                    break
            if memo_idx != -1:
                for j in range(memo_idx + 1, len(raw_tokens)):
                    token = raw_tokens[j]
                    if is_stop(token):
                        break
                    memo_parts.append(token)

        if len(name_parts) < min_name_tokens:
            continue

        name = re.sub(r"\s+", " ", " ".join(name_parts)).strip(" '\".,;:-")
        memo = re.sub(r"\s+", " ", " ".join(memo_parts)).strip(" '\".,;:-")

        if not name:
            continue
        if memo:
            return f"{prefix} {direction} {name} for {memo}"
        return f"{prefix} {direction} {name}"
    return None


def _dedupe_repeated_prefix(tokens: List[str]) -> List[str]:
    if len(tokens) < 4:
        return tokens
    prefix = tokens[:2]
    for i in range(2, len(tokens) - 1):
        if tokens[i : i + 2] == prefix:
            return tokens[:i]
    return tokens


def _fuzzy_dedupe(tokens: List[str]) -> List[str]:
    if len(tokens) < 2:
        return tokens
    # Immediate repeated token (e.g., "CVS/PHARMACY CVS/PHARMACY")
    if tokens[0] == tokens[1]:
        return [tokens[0]]
    collapsed = _dedupe_repeated_prefix(tokens)

    # Generic OCR duplicate collapse:
    # if the first token repeats shortly after, keep only the first segment.
    anchor = collapsed[0]
    for i in range(1, min(len(collapsed), 6)):
        if collapsed[i] != anchor:
            continue
        head = collapsed[:i]
        if len(head) >= 2 and i + 1 < len(collapsed):
            # If head tail is a prefix of the repeated tail token,
            # it's likely a wrapped/truncated duplicate (e.g. UNIVER / UNIVERSIT).
            if collapsed[i + 1].startswith(head[-1]):
                return head[:-1] if len(head[:-1]) >= 1 else [head[0]]
        return head if head else [anchor]

    return collapsed


def _strip_terminal_numbers(token: str) -> Optional[str]:
    # Drop standalone "#" or numeric-heavy tokens.
    if token == "#":
        return None
    if token.startswith("#") and token[1:].isdigit() and len(token[1:]) >= 3:
        return None
    digits = sum(ch.isdigit() for ch in token)
    if digits >= 3 and digits == len(token):
        return None
    # Remove trailing digits attached to words (e.g., BAGELS290 -> BAGELS).
    if digits >= 3 and digits < len(token):
        return re.sub(r"\d+$", "", token)
    return token


def _is_processor_prefix(token: str) -> bool:
    upper = token.upper().rstrip("*")
    for rule in _cfg().MERCHANT_PROCESSOR_PREFIXES:
        r = rule.upper().strip()
        if not r:
            continue
        if r.endswith("*"):
            if upper.startswith(r[:-1]):
                return True
            continue
        if upper == r:
            return True
    return False


def _strip_processor_prefix_tokens(tokens: List[str]) -> List[str]:
    out = list(tokens)
    while len(out) > 1 and _is_processor_prefix(out[0]):
        out.pop(0)
    return out


def _is_url_token(token: str) -> bool:
    lower = token.lower()
    if lower.startswith("www."):
        return True
    for tld in _cfg().MERCHANT_URL_TLD_STRIP:
        if re.search(rf"\.{re.escape(tld)}(/\S*)?$", lower):
            return True
    return False


def _extract_domain_from_url_token(token: str) -> Optional[str]:
    lower = token.lower()
    if lower.startswith("www."):
        lower = lower[4:]
    domain = lower.split(".")[0]
    if len(domain) >= 3 and any(c.isalpha() for c in domain):
        return domain.capitalize()
    return None


def _strip_url_tokens(tokens: List[str]) -> List[str]:
    url_domains: List[str] = []
    non_url: List[str] = []
    for t in tokens:
        if _is_url_token(t):
            domain = _extract_domain_from_url_token(t)
            if domain:
                url_domains.append(domain)
        else:
            non_url.append(t)

    if not non_url:
        # All tokens were URLs — use extracted domains or fall back to originals
        return url_domains if url_domains else tokens

    if url_domains:
        # If remaining non-URL tokens are entirely location/state/descriptor noise, prefer the domain
        meaningful = [
            t for t in non_url
            if not _is_state_abbr(t.upper())
            and t.lower() not in _cfg().LOCATION_WORDS
            and t.upper() not in _cfg().TRAILING_MERCHANT_DESCRIPTORS
        ]
        if not meaningful:
            return url_domains

    return non_url


def _is_tail_junk_token(token: str) -> bool:
    upper = token.upper()
    for pattern in _cfg().MERCHANT_TAIL_JUNK_PATTERNS:
        try:
            if re.fullmatch(pattern, upper):
                return True
        except re.error:
            continue
    return False


def _strip_tail_junk_tokens(tokens: List[str]) -> List[str]:
    out = list(tokens)
    while len(out) > 1 and _is_tail_junk_token(out[-1]):
        out.pop()
    return out


def _is_high_quality_merchant_token(token: str) -> bool:
    upper = token.upper()
    if upper in _cfg().MERCHANT_CONNECTOR_TOKENS:
        return False
    if token.isdigit():
        return False
    if _is_tail_junk_token(token):
        return False
    alpha_count = sum(ch.isalpha() for ch in token)
    if alpha_count == 0:
        return False
    if len(token) >= 2:
        return True
    return upper in _cfg().MERCHANT_SHORT_TOKEN_KEEP


def _strip_low_quality_tail_tokens(tokens: List[str]) -> List[str]:
    out = list(tokens)
    while len(out) > 1:
        tail = out[-1]
        upper_tail = tail.upper()
        if upper_tail in _cfg().MERCHANT_CONNECTOR_TOKENS:
            out.pop()
            continue
        if len(tail) == 1 and upper_tail not in _cfg().MERCHANT_SHORT_TOKEN_KEEP:
            out.pop()
            continue
        if len(tail) == 2 and tail.isalpha() and upper_tail not in _cfg().MERCHANT_SHORT_TOKEN_KEEP:
            out.pop()
            continue
        if _is_tail_junk_token(tail):
            out.pop()
            continue
        break
    return out


def _strip_single_char_noise_tokens(tokens: List[str]) -> List[str]:
    out: List[str] = []
    for token in tokens:
        upper = token.upper()
        if len(token) == 1 and upper not in _cfg().MERCHANT_SHORT_TOKEN_KEEP and upper not in _cfg().MERCHANT_CONNECTOR_TOKENS:
            continue
        out.append(token)
    return out


def _select_best_merchant_span(tokens: List[str], min_quality: int = 2, max_quality: int = 4) -> List[str]:
    if not tokens:
        return tokens
    quality_positions = [i for i, tok in enumerate(tokens) if _is_high_quality_merchant_token(tok)]
    if len(quality_positions) < min_quality:
        return tokens
    cut_pos = quality_positions[min(max_quality, len(quality_positions)) - 1]
    span = tokens[: cut_pos + 1]
    return _strip_low_quality_tail_tokens(span)


def _strip_location_tail(tokens: List[str]) -> List[str]:
    if not tokens:
        return tokens

    original = list(tokens)
    lowered = [t.lower() for t in tokens]

    phrases = sorted(
        _cfg().LOCATION_PHRASES,
        key=lambda p: len(p.split()),
        reverse=True,
    )
    for phrase in phrases:
        parts = phrase.lower().split()
        if len(parts) <= len(lowered) and lowered[-len(parts) :] == parts:
            tokens = tokens[: -len(parts)]
            lowered = lowered[: -len(parts)]
            break

    # Strip trailing state code if present.
    if tokens and _is_state_abbr(tokens[-1].upper()):
        tokens = tokens[:-1]
        lowered = lowered[:-1]

    if tokens and tokens[-1].lower() in _cfg().LOCATION_WORDS:
        tokens = tokens[:-1]

    # Remove orphan location prefix tokens left at the end
    # (e.g., "NEW" after stripping "YORK"/state from "NEW YORK ...").
    while len(tokens) > 1 and tokens[-1].lower() in _cfg().LOCATION_PREFIX_WORDS:
        tokens = tokens[:-1]

    return tokens if tokens else original


_CITY_NAMES_CACHE: Optional[set] = None
_CITY_NAMES_CACHE_LOADED: bool = False
_CITY_NAMES_LOCK = threading.Lock()


def _load_city_names() -> set:
    global _CITY_NAMES_CACHE, _CITY_NAMES_CACHE_LOADED
    with _CITY_NAMES_LOCK:
        if _CITY_NAMES_CACHE_LOADED:
            return _CITY_NAMES_CACHE or set()
        _CITY_NAMES_CACHE_LOADED = True
        try:
            import geonamescache  # type: ignore
            gc = geonamescache.GeonamesCache()
            _CITY_NAMES_CACHE = {c["name"].lower() for c in gc.get_cities().values()}
        except Exception:
            _CITY_NAMES_CACHE = set()
        return _CITY_NAMES_CACHE or set()


def _strip_city_state_tail(tokens: List[str]) -> List[str]:
    """Strip trailing city/state tokens using geonamescache + compact CITYSTATE detection.

    Handles two patterns missed by the existing location stripper:
      1. Compact merged tokens: "FRANCISCOCA", "Carmel-by-theCA" (city + 2-letter state, no space)
      2. Multi-word city names not in the manual LOCATION_WORDS config list
    """
    if len(tokens) <= 1:
        return tokens
    city_names = _load_city_names()
    state_abbrs = _cfg().STATE_ABBRS
    result = list(tokens)

    changed = True
    while changed and len(result) > 1:
        changed = False
        last = result[-1]

        # Pattern 1: compact CITYSTATE token (e.g. "FRANCISCOCA", "Carmel-by-theCA")
        if len(last) > 3 and last[-2:].upper() in state_abbrs:
            prefix = last[:-2]
            if len(prefix) >= 3:
                result.pop()
                changed = True
                continue

        # Pattern 2: last 1–3 tokens form a known city name (min 4 chars to avoid
        # false positives on short common words like "Of" which exist in geonamescache).
        for n in range(min(3, len(result) - 1), 0, -1):
            candidate = " ".join(result[-n:]).lower()
            if candidate in city_names and len(candidate) >= 4:
                del result[-n:]
                changed = True
                break

    return result if result else tokens


def _strip_state_zip_anchor(tokens: List[str]) -> List[str]:
    # Strip everything from the last state abbreviation to the end, but only search
    # within the last 3 tokens. This prevents stripping state abbrs embedded in merchant
    # names (e.g. "SD" in "SD ZOO ADMISSIONS", "CA" in "CA DMV FEE").
    # Also never returns an empty list — if stripping would leave nothing, keep original.
    if len(tokens) < 2:
        return tokens
    tail_start = max(0, len(tokens) - 3)
    for i in range(len(tokens) - 1, tail_start - 1, -1):
        if _is_state_abbr(tokens[i].upper()):
            result = tokens[:i]
            return result if result else tokens
    return tokens


def _normalize_alias_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", text.upper())).strip()


def _strip_location_like_merchant_tail(parts: List[str]) -> List[str]:
    if not parts:
        return parts

    compact_locations = {
        re.sub(r"[^A-Za-z0-9]", "", phrase).upper()
        for phrase in _cfg().LOCATION_PHRASES
        if phrase.strip()
    }

    def compact(token: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", token).upper()

    out = list(parts)
    while out:
        tail = compact(out[-1])
        if not tail:
            out.pop()
            continue
        # Exact compact location token, e.g. "LAJOLLA".
        if tail in compact_locations:
            out.pop()
            continue
        # State+location compact token, e.g. "CALAJOLLA".
        stripped = False
        for st in _cfg().STATE_ABBRS:
            if tail.startswith(st) and tail[len(st) :] in compact_locations:
                out.pop()
                stripped = True
                break
        if stripped:
            continue
        break
    return out


def _apply_merchant_normalization(name: str) -> str:
    if not name.strip():
        return name

    parts = [p for p in name.split() if p]
    if not parts:
        return name

    # Remove known branch/suffix tokens at tail.
    while parts and parts[-1].upper() in _merch_cfg().STRIP_SUFFIX_TOKENS:
        parts.pop()

    # Remove compact location-like tails (e.g., CALaJolla) using configured locations.
    parts = _strip_location_like_merchant_tail(parts)

    # Token-level replacements.
    replaced_parts: List[str] = []
    for part in parts:
        replacement = _merch_cfg().TOKEN_REPLACEMENTS.get(part.upper())
        if replacement:
            replaced_parts.extend(replacement.split())
        else:
            replaced_parts.append(part)

    normalized_text = " ".join(replaced_parts).strip()
    alias_key = _normalize_alias_key(normalized_text)

    if alias_key in _merch_cfg().ALIASES:
        return _merch_cfg().ALIASES[alias_key]

    for key, canonical in _merch_cfg().CONTAINS_ALIASES.items():
        if key and key in alias_key:
            return canonical

    return re.sub(r"\s+", " ", normalized_text).strip()


_REFERENCE_COMPANIES: Optional[List[str]] = None
_REFERENCE_COMPANIES_LOADED: bool = False
_REFERENCE_COMPANIES_LOCK = threading.Lock()
_DEFAULT_REFERENCE_CSV = PROJECT_ROOT / "categorizer" / "datasets" / "hardcoded_companies_for_classification.csv"
_FUZZY_NORMALIZE_THRESHOLD = 90


def _load_reference_companies() -> List[str]:
    global _REFERENCE_COMPANIES, _REFERENCE_COMPANIES_LOADED
    with _REFERENCE_COMPANIES_LOCK:
        if _REFERENCE_COMPANIES_LOADED:
            return _REFERENCE_COMPANIES or []
        _REFERENCE_COMPANIES_LOADED = True
        csv_path = Path(os.environ.get("RAPIDFUZZ_REFERENCE_CSV", str(_DEFAULT_REFERENCE_CSV)))
        if not csv_path.exists():
            _REFERENCE_COMPANIES = []
            return []
        try:
            companies: List[str] = []
            with csv_path.open("r", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    company = str(row.get("Company", "")).strip()
                    if company:
                        companies.append(company)
            _REFERENCE_COMPANIES = companies
        except Exception:
            _REFERENCE_COMPANIES = []
        return _REFERENCE_COMPANIES or []


def _fuzzy_normalize_merchant(merchant: str) -> str:
    """If merchant fuzzy-matches a known canonical company at high confidence, return the canonical name."""
    if len(merchant.strip()) < 3:
        return merchant
    # Preserve Zelle/Venmo transfers — recipient name is meaningful
    m_lower = merchant.lower()
    if m_lower.startswith("zelle") or m_lower.startswith("venmo"):
        return merchant
    companies = _load_reference_companies()
    if not companies:
        return merchant
    try:
        from rapidfuzz import process, fuzz, utils  # type: ignore
        result = process.extractOne(
            merchant, companies, scorer=fuzz.WRatio, processor=utils.default_process
        )
        if result is None or result[1] < _FUZZY_NORMALIZE_THRESHOLD:
            return merchant
        canonical = result[0]
        # Guard: canonical name must be at least 50% the length of the query (no spaces).
        # Prevents short names like "Mobil" matching inside longer strings like "TMOBILE AUTO".
        q_ns = merchant.replace(" ", "")
        c_ns = canonical.replace(" ", "")
        if len(c_ns) < len(q_ns) * 0.5:
            return merchant
        return canonical
    except Exception:
        pass
    return merchant


def _is_good_merchant_candidate(text: str) -> bool:
    if len(text.strip()) < 3:
        return False
    words = re.findall(r"[A-Za-z]+", text)
    if len(words) >= 2:
        return True
    return len(words) == 1 and len(words[0]) >= 4


def _extract_structured_merchant(raw: str) -> Optional[str]:
    labels = [label.strip() for label in _cfg().METADATA_LABELS if label.strip()]
    if not labels:
        return None

    escaped = [re.escape(label) for label in sorted(labels, key=len, reverse=True)]
    label_re = re.compile(rf"\b(?P<label>{'|'.join(escaped)})\s*:?", re.IGNORECASE)
    matches = list(label_re.finditer(raw))
    if not matches:
        return None

    first_idx = matches[0].start()
    prefix = raw[:first_idx].strip()
    fields: dict[str, str] = {}
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        label = match.group("label").upper()
        value = raw[start:end].strip()
        if value:
            fields[label] = value

    prefix_clean = safe_cleanup(prefix)
    if _is_good_merchant_candidate(prefix_clean):
        return prefix_clean

    for preferred in _cfg().PREFERRED_MERCHANT_LABELS:
        value = fields.get(preferred.upper())
        if not value:
            continue
        candidate = safe_cleanup(value)
        if _is_good_merchant_candidate(candidate):
            return candidate

    for value in fields.values():
        candidate = safe_cleanup(value)
        if _is_good_merchant_candidate(candidate):
            return candidate

    return None


def _refine_merchant(name: str, fallback: str) -> tuple[str, Optional[str]]:
    # Fee detection takes precedence (config-driven labels).
    upper = name.upper()
    for key in _cfg().FEE_KEYWORDS:
        if key in upper:
            label = _cfg().FEE_LABELS.get(key, key.title())
            return label, "fee"

    stage = name.strip()
    is_p2p_formatted = False
    for rule in _cfg().P2P_RULES:
        prefix = str(rule.get("prefix", "")).strip().lower()
        if prefix and stage.lower().startswith(prefix + " "):
            is_p2p_formatted = True
            break

    tokens = []
    for raw_token in stage.split():
        token = _strip_token_punct(raw_token)
        if not token:
            continue
        if token == "&":
            continue
        token = _strip_terminal_numbers(token)
        if not token:
            continue
        tokens.append(token)

    tokens = _strip_processor_prefix_tokens(tokens)
    tokens = _strip_url_tokens(tokens)
    tokens = _strip_tail_junk_tokens(tokens)
    tokens = _strip_low_quality_tail_tokens(tokens)

    if not is_p2p_formatted:
        tokens = _strip_state_zip_anchor(tokens)
        tokens = _strip_location_tail(tokens)
        tokens = _strip_city_state_tail(tokens)
        tokens = _strip_tail_junk_tokens(tokens)
        tokens = _strip_low_quality_tail_tokens(tokens)
        tokens = _select_best_merchant_span(tokens)
        tokens = _strip_single_char_noise_tokens(tokens)
        tokens = _strip_low_quality_tail_tokens(tokens)
    tokens = _fuzzy_dedupe(tokens)

    cleaned = " ".join(tokens).strip()
    if len(cleaned) < 3:
        cleaned = fallback.strip()
    return cleaned, None


def merchant_clean(raw: str) -> str:
    stage_a = _truncate_at_garbage(raw)
    structured = _extract_structured_merchant(stage_a)
    stage_b = _stage_b_extract(structured or stage_a)
    cleaned = safe_cleanup(stage_b)
    refined, txn_subtype = _refine_merchant(cleaned, structured or stage_b or stage_a)
    refined = _strip_trailing_descriptors(refined)
    normalized = _apply_merchant_normalization(refined)
    # Skip fuzzy normalization for fee-type results — the fee label is already canonical.
    if not txn_subtype:
        normalized = _fuzzy_normalize_merchant(normalized)
    if len(normalized) > 60:
        return normalized[:57].rstrip() + "..."
    return normalized


def _apply_venmo_direction(merchant: str, raw_description: str, txn_type: Optional[str]) -> str:
    raw_upper = raw_description.upper()
    if "VENMO" not in raw_upper:
        return merchant

    merch_norm = re.sub(r"\s+", " ", merchant).strip()
    merch_lower = merch_norm.lower()
    if "venmo to " in merch_lower or "venmo from " in merch_lower:
        return merch_norm

    name = re.sub(r"^\s*VENMO\b", "", merch_norm, flags=re.IGNORECASE).strip(" -*")
    if not name:
        return "Venmo"

    if txn_type == "withdrawals":
        return f"Venmo to {name}"
    if txn_type == "deposits":
        return f"Venmo from {name}"
    return merch_norm


def _detect_payment_channel(raw_description: str) -> str:
    text = raw_description.upper()
    for needle, label in _cfg().PAYMENT_CHANNEL_RULES:
        if needle in text:
            return label
    return "NA"


def _extract_transaction_code(raw_description: str) -> str:
    text = raw_description.upper()
    for pattern in _cfg().TRANSACTION_CODE_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(0).replace(":", "").replace("*", "")
    return "NA"


def _extract_city_state_zip(raw_description: str) -> tuple[str, str, str]:
    text = re.sub(r"\s+", " ", raw_description).strip()
    if not text:
        return "NA", "NA", "NA"

    def strip_metadata_tail(value: str) -> str:
        # Location should come from merchant/location tail, not metadata segments
        # like "DES:...", "INDN:...", "CO ID:...".
        cut_idx: Optional[int] = None
        for label in _cfg().METADATA_LABELS:
            token = str(label).strip()
            if not token:
                continue
            pattern = re.compile(rf"\b{re.escape(token)}\s*:", flags=re.IGNORECASE)
            match = pattern.search(value)
            if not match:
                continue
            cut_idx = match.start() if cut_idx is None else min(cut_idx, match.start())
        return value[:cut_idx].strip() if cut_idx is not None else value

    original_text = text
    text = strip_metadata_tail(text)
    text = _strip_trailing_descriptors(text)

    def _phrase_regex(phrase: str) -> re.Pattern[str]:
        # Conservative abbreviation handling only for configured location phrases.
        # Example: "Universal City" should match "... UNIVERSAL CIT ...".
        parts = [p for p in re.split(r"\s+", phrase.strip()) if p]
        pattern_parts: List[str] = []
        for part in parts:
            if part.upper() == "CITY":
                pattern_parts.append(r"(?:CITY|CIT)")
            else:
                pattern_parts.append(re.escape(part))
        pattern = r"(?<!\w)" + r"\s+".join(pattern_parts) + r"(?!\w)"
        return re.compile(pattern, flags=re.IGNORECASE)

    def _normalize_location_text(value: str) -> str:
        # Keep this minimal to avoid altering existing parsing behavior.
        return re.sub(r"\bCIT\b", "CITY", value, flags=re.IGNORECASE)

    zip_code = "NA"
    state = "NA"
    city = "NA"

    # Prefer right-most known location phrase that leaves only optional state/zip at tail.
    lowered = text.lower()
    best_match: tuple[int, int, str, str, str] | None = None  # (start_idx, length, city, state, zip)
    for phrase in _cfg().LOCATION_PHRASES:
        p = phrase.strip()
        if not p:
            continue
        phrase_re = _phrase_regex(p)
        for m in phrase_re.finditer(text):
            tail = text[m.end() :].strip()
            tail_match = re.fullmatch(r"(?:(?P<state>[A-Z]{2}))?(?:\s+(?P<zip>\d{5}(?:-\d{4})?))?[\s]*", tail)
            if not tail_match:
                continue
            candidate_state = (tail_match.group("state") or "").upper()
            candidate_zip = tail_match.group("zip") or "NA"
            if candidate_state and candidate_state not in _cfg().STATE_ABBRS:
                continue
            candidate = (m.start(), len(p), p, candidate_state if candidate_state else "NA", candidate_zip)
            if best_match is None or candidate[0] > best_match[0] or (
                candidate[0] == best_match[0] and candidate[1] > best_match[1]
            ):
                best_match = candidate
    if best_match is not None:
        return best_match[2], best_match[3], best_match[4]

    # Compact tail match for OCR/PDF-joined city tokens (e.g. "LAJOLLA", "SANDIEGO"),
    # while still honoring optional trailing state/zip tokens.
    tail_tokens = re.findall(r"[A-Za-z0-9-]+", text)
    tail_state = "NA"
    tail_zip = "NA"
    work = list(tail_tokens)
    if work and re.fullmatch(r"\d{5}(?:-\d{4})?", work[-1]):
        tail_zip = work[-1]
        work = work[:-1]
    if work and work[-1].upper() in _cfg().STATE_ABBRS:
        tail_state = work[-1].upper()
        work = work[:-1]
    compact_city_tail = re.sub(r"[^A-Za-z0-9]", "", "".join(work[-3:]).upper()) if work else ""
    if compact_city_tail.endswith("CIT"):
        compact_city_tail = compact_city_tail[:-3] + "CITY"
    if compact_city_tail:
        for phrase in sorted(_cfg().LOCATION_PHRASES, key=len, reverse=True):
            p = phrase.strip()
            if not p:
                continue
            compact_phrase = re.sub(r"[^A-Za-z0-9]", "", p).upper()
            if compact_phrase and compact_city_tail.endswith(compact_phrase):
                return p, tail_state, tail_zip
        if tail_state == "NA":
            for st in _cfg().STATE_ABBRS:
                if not compact_city_tail.endswith(st):
                    continue
                city_compact = compact_city_tail[: -len(st)]
                if not city_compact:
                    continue
                for phrase in sorted(_cfg().LOCATION_PHRASES, key=len, reverse=True):
                    compact_phrase = re.sub(r"[^A-Za-z0-9]", "", phrase).upper()
                    if compact_phrase and city_compact.endswith(compact_phrase):
                        return phrase, st, tail_zip

    # Tail match only: CITY STATE [ZIP] at end of description.
    tail_match = re.search(
        r"\b(?P<city>[A-Za-z][A-Za-z .'-]{1,40}?)\s+(?P<state>[A-Z]{2})(?:\s+(?P<zip>\d{5}(?:-\d{4})?))?[\s]*$",
        text,
    )
    if tail_match:
        candidate_state = tail_match.group("state").upper()
        if candidate_state in _cfg().STATE_ABBRS:
            state = candidate_state
            city = re.sub(r"\s+", " ", tail_match.group("city")).strip(" ,.-")
            zip_group = tail_match.group("zip")
            zip_code = zip_group if zip_group else "NA"
            city_tokens = [tok for tok in city.split() if tok]
            city_is_known_single = len(city_tokens) == 1 and city_tokens[0].lower() in _cfg().LOCATION_WORDS
            known_phrase_set = {p.lower() for p in _cfg().LOCATION_PHRASES}
            city_is_known_phrase = city.lower() in known_phrase_set
            if len(city_tokens) < 2 and not city_is_known_single and zip_code == "NA":
                return "NA", "NA", "NA"
            # Prevent payer-name tails from being interpreted as city/state, especially
            # in metadata-heavy rows like "... INDN: FIRST LAST CO".
            if "INDN:" in original_text.upper() and not city_is_known_single and not city_is_known_phrase:
                return "NA", "NA", "NA"
            return (city if city else "NA"), state, zip_code

    # Fallback: known location phrase at end (no state/zip present).
    normalized_tail_text = _normalize_location_text(lowered)
    for phrase in sorted(_cfg().LOCATION_PHRASES, key=len, reverse=True):
        p = phrase.strip()
        if p and normalized_tail_text.endswith(_normalize_location_text(p.lower())):
            return p, "NA", "NA"

    # Last-token merge fallback (e.g. "... LA JOLLA" as separate words at tail).
    tokens = re.findall(r"[A-Za-z0-9]+", text.upper())
    tail_window = tokens[-4:] if len(tokens) >= 4 else tokens
    for n in (3, 2):
        if len(tail_window) < n:
            continue
        merged = "".join(tail_window[-n:])
        if merged.endswith("CIT"):
            merged = merged[:-3] + "CITY"
        for phrase in _cfg().LOCATION_PHRASES:
            compact_phrase = re.sub(r"[^A-Za-z0-9]", "", phrase).upper()
            if compact_phrase and merged == compact_phrase:
                return phrase, "NA", "NA"

    # Fallback: trailing standalone state code (city unknown).
    tail_state_only = re.search(r"\b([A-Z]{2})\b\s*$", text.upper())
    if tail_state_only:
        candidate_state = tail_state_only.group(1)
        if candidate_state in _cfg().STATE_ABBRS:
            return "NA", candidate_state, "NA"

    # Fallback: state code merged into domain-like token suffix (e.g. .COMCA).
    domainish_tokens = re.findall(r"[A-Za-z0-9./-]+", text)
    for token in reversed(domainish_tokens):
        upper_tok = token.upper()
        suffix = upper_tok[-2:]
        if suffix not in _cfg().STATE_ABBRS:
            continue
        prefix = upper_tok[:-2]
        if not prefix:
            continue
        if (("." in prefix) or ("/" in prefix)) and re.search(r"[A-Z]", prefix):
            return "NA", suffix, "NA"

    return "NA", "NA", "NA"


def _extract_reference_id(raw_description: str, account_last4: Optional[str] = None) -> str:
    text = raw_description
    phone_re = re.compile(r"^\+?\d{1,3}[-.\s]?\d{3}[-.\s]?\d{3}[-.\s]?\d{4}$")
    acct_last4 = re.sub(r"\D", "", str(account_last4 or ""))

    def is_phone_like(value: str) -> bool:
        token = value.strip()
        if phone_re.fullmatch(token):
            return True
        digits_only = re.sub(r"\D", "", token)
        return len(digits_only) == 10 and token != digits_only

    def valid_candidate(candidate: str) -> bool:
        token = candidate.strip()
        if not token:
            return False
        if is_phone_like(token):
            return False
        digits_only = re.sub(r"\D", "", token)
        is_numeric = token.isdigit()
        # Numeric IDs shorter than 6 are typically last4/card fragments.
        if is_numeric and len(digits_only) < 6:
            return False
        # Never use account last4 as transaction reference id.
        if acct_last4 and token == acct_last4:
            return False
        return True

    # Prefer first transaction-level ID and ignore company-level "CO ID".
    id_pattern = re.compile(r"\b(?:CO\s+)?ID:?\s*([A-Z0-9-]{4,})\b", flags=re.IGNORECASE)
    for match in id_pattern.finditer(text):
        full = match.group(0)
        if re.match(r"^\s*CO\s+ID:?", full, flags=re.IGNORECASE):
            continue
        candidate = match.group(1)
        if valid_candidate(candidate):
            return candidate

    conf_match = re.search(r"\bCONF#?\s*([A-Z0-9-]{4,})\b", text, flags=re.IGNORECASE)
    if conf_match:
        candidate = conf_match.group(1)
        if valid_candidate(candidate):
            return candidate

    # Hash-prefixed numeric refs (e.g. "#00009875"): drop '#' but keep leading zeros.
    hash_ref = re.search(r"#\s*([0-9]{4,})\b", text)
    if hash_ref:
        candidate = hash_ref.group(1)
        # Hash-prefixed numeric refs are often merchant/store transaction IDs.
        # Allow 5+ digits here (keeping leading zeros), while still rejecting account last4.
        if len(candidate) >= 5 and (not acct_last4 or candidate != acct_last4):
            return candidate

    # Company identifiers are metadata, not transaction-level references.
    text_no_company_ids = re.sub(r"\bCO\s+ID:?\s*[A-Z0-9-]{4,}\b", " ", text, flags=re.IGNORECASE)

    # Fallback to ID-like tokens only (must contain digits), avoiding plain words.
    tokens = re.findall(r"\b[A-Z0-9-]{8,}\b", text_no_company_ids.upper())
    for token in tokens:
        digit_count = sum(ch.isdigit() for ch in token)
        if digit_count >= 2 and valid_candidate(token):
            return token
    return "NA"


def _strip_city_overlap_from_merchant(merchant: str, city: str) -> str:
    if not merchant.strip() or city == "NA":
        return merchant
    merchant_tokens = merchant.split()
    city_tokens = city.split()
    if not merchant_tokens or not city_tokens:
        return merchant

    def norm(token: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", token).upper()

    city_norm_tokens = [norm(t) for t in city_tokens if norm(t)]
    if not city_norm_tokens:
        return merchant
    city_prefixes = set(city_norm_tokens[:-1])

    # Exact city-suffix removal first (e.g., "... SAN DIEGO" -> "...").
    if len(merchant_tokens) >= len(city_norm_tokens):
        tail = [norm(t) for t in merchant_tokens[-len(city_norm_tokens) :]]
        if tail == city_norm_tokens:
            trimmed = " ".join(merchant_tokens[: -len(city_norm_tokens)]).strip()
            if trimmed:
                return trimmed

    city_last = city_norm_tokens[-1]
    changed = False
    while merchant_tokens:
        tail = norm(merchant_tokens[-1])
        if not tail:
            merchant_tokens.pop()
            changed = True
            continue
        if tail == city_last:
            merchant_tokens.pop()
            changed = True
            continue
        if tail in city_prefixes:
            merchant_tokens.pop()
            changed = True
            continue
        # Handles compact prefixes like MAD -> MADRID, NEW -> NEWYORK tail token.
        if len(tail) >= 3 and city_last.startswith(tail):
            merchant_tokens.pop()
            changed = True
            continue
        break

    cleaned = " ".join(merchant_tokens).strip()
    return cleaned if (changed and cleaned) else merchant


def _strip_trailing_descriptors(text: str) -> str:
    if not text.strip() or not _cfg().TRAILING_MERCHANT_DESCRIPTORS:
        return text
    out = text.strip()
    changed = True
    while changed and out:
        changed = False
        upper = out.upper().strip()
        for desc in sorted(_cfg().TRAILING_MERCHANT_DESCRIPTORS, key=len, reverse=True):
            if upper.endswith(desc):
                out = out[: len(out) - len(desc)].strip(" ,.-/")
                changed = True
                break
    return out.strip() or text


def _is_non_transaction_row(text: str) -> bool:
    if not text.strip() or not _cfg().NON_TRANSACTION_PATTERNS:
        return False
    for pattern in _cfg().NON_TRANSACTION_PATTERNS:
        try:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return True
        except re.error:
            if pattern.lower() in text.lower():
                return True
    return False


def _statement_range_slug(statement_period: Optional[str], pdf_path: Path) -> str:
    if not statement_period:
        return pdf_path.stem
    match = re.search(r"([A-Za-z]+ \d{1,2}, \d{4}) to ([A-Za-z]+ \d{1,2}, \d{4})", statement_period)
    if not match:
        return pdf_path.stem
    try:
        start = datetime.strptime(match.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
        end = datetime.strptime(match.group(2), "%B %d, %Y").strftime("%Y-%m-%d")
        return f"{start}_to_{end}"
    except ValueError:
        return pdf_path.stem


CSV_COLUMNS = [
    "date",
    "payment_channel",
    "transaction_code",
    "merchant_raw",
    "merchant_clean",
    "city",
    "state",
    "zip",
    "amount",
    "direction",
    "reference_id",
    "source_file",
    "category",
]


def _sanitize_slug(value: str, fallback: str = "unknown") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").lower()
    return cleaned or fallback


def _mask_account_number(account_number: Optional[str]) -> str:
    if not account_number:
        return "unknown"
    digits = re.sub(r"\D", "", account_number)
    if not digits:
        return "unknown"
    return digits[-4:] if len(digits) >= 4 else digits


def _first_last_slug(*candidates: Optional[str]) -> str:
    for value in candidates:
        if not value:
            continue
        tokens = re.findall(r"[A-Za-z0-9]+", str(value))
        if len(tokens) >= 2:
            return _sanitize_slug(f"{tokens[0]}_{tokens[1]}", fallback="unknown_user")
    for value in candidates:
        if not value:
            continue
        cleaned = _sanitize_slug(str(value), fallback="")
        if cleaned:
            return cleaned
    return "unknown_user"


def _bank_short_slug(bank_name: Optional[str], original_name: Optional[str] = None) -> str:
    candidates: List[str] = []
    if isinstance(bank_name, str) and bank_name.strip():
        candidates.append(bank_name)
    candidates.extend(_cfg().BANK_ALIASES or [])

    normalized: List[str] = []
    for c in candidates:
        token = re.sub(r"[^a-z0-9]+", "", str(c).lower())
        if token:
            normalized.append(token)

    preferred = [
        c
        for c in normalized
        if 3 <= len(c) <= 8 and c not in {"bank", "unknown", "unknownbank", "unknownb"}
    ]
    if preferred:
        return sorted(preferred, key=lambda x: (len(x), x))[0]

    if original_name:
        stem = Path(original_name).stem.lower()
        tokens = re.findall(r"[a-z]+", stem)
        for token in tokens:
            if len(token) >= 3:
                return token[:8]

    if normalized:
        value = sorted(normalized, key=lambda x: (len(x), x))[0]
        return value[:8] if len(value) > 8 else value
    return "bank"


def _build_csv_rows(payload: dict, source_file: str) -> List[dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    rows: List[dict[str, str]] = []
    account_number = payload.get("statement_info", {}).get("account_number")
    account_last4 = _mask_account_number(str(account_number)) if account_number else ""
    for txn in payload.get("transactions", []):
        date = str(txn.get("date") or "NA")
        raw_description = str(txn.get("raw_description") or "")
        merchant = str(txn.get("merchant_clean") or "NA")
        amount = str(txn.get("amount") or "NA")
        if amount.upper() == "UNPARSED":
            continue
        if _parse_amount(amount) is None:
            continue

        dedupe_key = (date, raw_description, amount)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        city, state, zip_code = _extract_city_state_zip(raw_description)
        merchant = _strip_trailing_descriptors(merchant)
        merchant = _strip_city_overlap_from_merchant(merchant, city)
        parsed_amount = _parse_amount(amount)
        direction = "NA"
        if parsed_amount is not None:
            direction = "credit" if parsed_amount >= 0 else "debit"

        rows.append(
            {
                "date": date,
                "payment_channel": _detect_payment_channel(raw_description),
                "transaction_code": _extract_transaction_code(raw_description),
                "merchant_raw": raw_description if raw_description.strip() else "NA",
                "merchant_clean": merchant if merchant.strip() else "NA",
                "city": city if city else "NA",
                "state": state if state else "NA",
                "zip": zip_code if zip_code else "NA",
                "amount": amount,
                "direction": direction,
                "reference_id": _extract_reference_id(raw_description, account_last4=account_last4),
                "source_file": source_file if source_file.strip() else "NA",
                "category": str(txn.get("category") or "Uncategorized"),
            }
        )
    return rows


def _dedupe_hash_key(row: dict[str, str], account_scope: str = "") -> str:
    merchant_raw = str(row.get("merchant_raw", "NA") or "NA")
    merchant_norm = re.sub(r"[^a-z0-9]+", " ", merchant_raw.lower()).strip()
    date_norm = str(row.get("date", "NA") or "NA").strip()
    ref_norm = str(row.get("reference_id", "NA") or "NA").strip().upper()

    amount_raw = str(row.get("amount", "NA") or "NA").strip()
    amount_parsed = _parse_amount(amount_raw)
    amount_norm = f"{amount_parsed:.2f}" if amount_parsed is not None else amount_raw

    key = "|".join([account_scope, date_norm, merchant_norm, amount_norm, ref_norm])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _write_csv_rows(csv_path: Path, rows: List[dict[str, str]], append: bool, account_scope: str = "") -> None:
    normalized_rows = [{k: (v if str(v).strip() else "NA") for k, v in row.items()} for row in rows]

    if not append or not csv_path.exists():
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for row in normalized_rows:
                writer.writerow(row)
        return

    # Append mode with strong dedupe + overwrite semantics (incoming rows win).
    merged: dict[str, dict[str, str]] = {}
    order: list[str] = []
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            row_clean = {k: (row.get(k, "NA") or "NA") for k in CSV_COLUMNS}
            key = _dedupe_hash_key(row_clean, account_scope=account_scope)
            if key not in merged:
                order.append(key)
            merged[key] = row_clean

    for row in normalized_rows:
        key = _dedupe_hash_key(row, account_scope=account_scope)
        if key not in merged:
            order.append(key)
        merged[key] = row

    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for key in order:
            writer.writerow(merged[key])


def _file_sha256(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def export_transactions_csv(payload: dict, pdf_path: Path) -> Path:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    period = payload.get("statement_info", {}).get("statement_period")
    base_name = _statement_range_slug(period, pdf_path)
    csv_path = PROCESSED_DIR / f"{base_name}_transactions.csv"
    rows = _build_csv_rows(payload, pdf_path.name)
    _write_csv_rows(csv_path, rows, append=False)

    return csv_path


def persist_uploaded_statement(
    payload: dict,
    original_filename: str,
    pdf_bytes: bytes,
    username: Optional[str] = None,
) -> dict[str, str]:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)

    statement = payload.get("statement_info", {})
    account_number = statement.get("account_number")
    owner_name = statement.get("owner_name")
    account_name = statement.get("account_name")
    user_slug = _first_last_slug(owner_name, account_name, username)
    account_slug = _mask_account_number(str(account_number) if account_number else None)

    user_root = DATA_ROOT / user_slug
    user_raw_dir = user_root / "raw"
    user_processed_dir = user_root / "processed"
    user_raw_dir.mkdir(parents=True, exist_ok=True)
    user_processed_dir.mkdir(parents=True, exist_ok=True)

    input_name = Path(original_filename).name if original_filename else "statement.pdf"
    if not input_name.lower().endswith(".pdf"):
        input_name = f"{Path(input_name).stem}.pdf"

    original_slug = _sanitize_slug(Path(input_name).stem, "statement")
    bank_short = _bank_short_slug(statement.get("bank_name"), input_name)
    base_pdf_name = f"{original_slug}_{bank_short}{account_slug}.pdf"
    saved_pdf_path = user_raw_dir / base_pdf_name
    incoming_hash = hashlib.sha256(pdf_bytes).hexdigest()
    reused_existing = False

    # Reuse existing identical PDF instead of creating duplicate copies.
    candidates = sorted(user_raw_dir.glob("*.pdf"))
    for candidate in candidates:
        existing_hash = _file_sha256(candidate)
        if existing_hash and existing_hash == incoming_hash:
            saved_pdf_path = candidate
            reused_existing = True
            break

    # If identical content exists under an old naming pattern, normalize filename now.
    if reused_existing and saved_pdf_path.name != base_pdf_name:
        normalized_target = user_raw_dir / base_pdf_name
        if not normalized_target.exists():
            try:
                saved_pdf_path.rename(normalized_target)
                saved_pdf_path = normalized_target
            except OSError:
                pass

    if not reused_existing:
        if saved_pdf_path.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            saved_pdf_path = user_raw_dir / f"{original_slug}_{bank_short}{account_slug}_{stamp}.pdf"
        saved_pdf_path.write_bytes(pdf_bytes)

    account_csv_name = f"acc_{account_slug}.csv"
    account_csv_path = user_processed_dir / account_csv_name
    rows = _build_csv_rows(payload, saved_pdf_path.name)
    _write_csv_rows(account_csv_path, rows, append=True, account_scope=account_slug)

    return {
        "saved_pdf_path": str(saved_pdf_path),
        "account_csv_path": str(account_csv_path),
        "pdf_reused": "1" if reused_existing else "0",
    }


@dataclass
class StatementInfo:
    bank_name: Optional[str] = None
    account_name: Optional[str] = None
    owner_name: Optional[str] = None
    statement_period: Optional[str] = None
    account_number: Optional[str] = None


@dataclass
class AccountSummary:
    beginning_balance: Optional[str] = None
    beginning_balance_date: Optional[str] = None
    deposits_additions: Optional[str] = None
    withdrawals_subtractions: Optional[str] = None
    checks: Optional[str] = None
    service_fees: Optional[str] = None
    ending_balance: Optional[str] = None
    ending_balance_date: Optional[str] = None


@dataclass
class Transaction:
    date: str
    description: str
    amount: str
    txn_type: Optional[str] = None


@dataclass
class ParseStats:
    reconstructed_rows: int = 0
    output_txns: int = 0
    section_sums: dict = None
    warnings: List[str] = None
    failed_rows: List[str] = None


def extract_text(pdf_path: Path) -> List[str]:
    try:
        import pdfplumber  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency path
        raise RuntimeError(
            "Missing dependency: pdfplumber. Install with: python3 -m pip install pdfplumber"
        ) from exc

    lines: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                cleaned = re.sub(r"\s+", " ", line).strip()
                if cleaned:
                    lines.append(cleaned)
    return lines


def _detect_config_path(lines: List[str], config_dir: Path) -> Optional[Path]:
    # Match bank aliases from any config JSON in the same directory
    # using token boundaries to avoid false positives (e.g., "boa" in "bonus").
    text = " ".join(lines).lower()
    best_path: Optional[Path] = None
    best_score = -1

    def alias_matches(alias: str) -> bool:
        escaped = re.escape(alias)
        pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
        return re.search(pattern, text) is not None

    for cfg_path in sorted(config_dir.glob("*.json")):
        try:
            with cfg_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        aliases = data.get("bank_aliases", [])
        if not isinstance(aliases, list):
            continue
        for alias in aliases:
            alias_str = str(alias).lower().strip()
            if not alias_str:
                continue
            if alias_matches(alias_str):
                score = len(alias_str)
                if score > best_score:
                    best_score = score
                    best_path = cfg_path
    return best_path


def _guess_bank_name_from_lines(lines: List[str]) -> Optional[str]:
    patterns = [
        re.compile(r"\b([A-Z][A-Za-z&.\- ]{2,40}\bBANK(?: OF [A-Z][A-Za-z]+)?)\b"),
        re.compile(r"\b([A-Z][A-Za-z&.\- ]{2,40}\bCREDIT UNION)\b"),
        re.compile(r"\b([A-Z][A-Za-z&.\- ]{2,40}\bFINANCIAL)\b"),
        re.compile(r"\bDISCOVER\b", re.IGNORECASE),
        re.compile(r"\bCHASE\b", re.IGNORECASE),
        re.compile(r"\bCITI(?:BANK)?\b", re.IGNORECASE),
        re.compile(r"\bWELLS FARGO\b", re.IGNORECASE),
        re.compile(r"\bAMERICAN EXPRESS\b", re.IGNORECASE),
        re.compile(r"\bCAPITAL ONE\b", re.IGNORECASE),
        re.compile(r"\bBANK OF AMERICA\b|\bBOFA\b|\bBOA\b", re.IGNORECASE),
    ]
    for line in lines[:160]:
        for pat in patterns:
            m = pat.search(line)
            if not m:
                continue
            if m.groups():
                return re.sub(r"\s+", " ", m.group(1)).strip(" -:")
            return m.group(0).strip()
    return None


def _config_basename_from_bank_name(bank_name: str) -> str:
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
    return tokens[0] if tokens else "unknown_bank"


def _infer_config_path_by_bank_name(lines: List[str], config_dir: Path) -> Optional[Path]:
    bank_name = _guess_bank_name_from_lines(lines)
    if not bank_name:
        return None
    base = _config_basename_from_bank_name(bank_name)
    candidate = config_dir / f"{base}_config.json"
    return candidate if candidate.exists() else None


def _parse_amount(value: str) -> Optional[float]:
    if not value:
        return None
    raw = value.strip()
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1]
    if raw.startswith("-"):
        negative = True
        raw = raw[1:]
    if raw.startswith("+"):
        raw = raw[1:]
    raw = raw.replace("$", "").replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        return None
    return -amount if negative else amount


def _has_explicit_sign(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith("-") or (stripped.startswith("(") and stripped.endswith(")"))


def _is_ignorable_tail(tail: str) -> bool:
    if not tail.strip():
        return True
    stripped = tail.strip()
    for key in _cfg().FEE_KEYWORDS:
        stripped = re.sub(re.escape(key), " ", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if not stripped:
        return True
    tokens = stripped.split()
    for token in tokens:
        if re.fullmatch(r"\+?\d{1,3}[-.\s]?\d{3}[-.\s]?\d{3}[-.\s]?\d{4}", token):
            continue
        if re.fullmatch(r"[0-9.,-]+", token):
            continue
        if re.fullmatch(r"#?[0-9]{6,}", token):
            continue
        if re.fullmatch(r"[A-Z0-9]{5,}", token.upper()) and any(ch.isdigit() for ch in token):
            continue
        # OCR often merges CITY+STATE (e.g., VALLEYCA, LAGUNABEACHCA).
        if re.fullmatch(r"[A-Z]{5,}", token.upper()) and _is_state_abbr(token.upper()[-2:]):
            continue
        ok = any(re.fullmatch(p, token, flags=re.IGNORECASE) for p in _cfg().IGNORABLE_TAIL_PATTERNS)
        if not ok and _is_state_abbr(token.upper()):
            ok = True
        if not ok:
            return False
    return True


def _is_redundant_tail(remainder: str, amount_match: re.Match[str]) -> bool:
    tail = remainder[amount_match.end() :].strip()
    if not tail:
        return True
    if _is_ignorable_tail(tail):
        return True

    head = remainder[: amount_match.start()].upper()
    head_tokens = set(re.findall(r"[A-Z0-9*'/.-]+", head))
    descriptor_tokens: set[str] = set()
    for desc in _cfg().TRAILING_MERCHANT_DESCRIPTORS:
        descriptor_tokens.update(re.findall(r"[A-Z0-9*'/.-]+", desc.upper()))

    for token in re.findall(r"[A-Z0-9*'/.-]+", tail.upper()):
        if _is_state_abbr(token):
            continue
        if re.fullmatch(r"\+?\d{1,3}[-.]?\d{3}[-.]?\d{3}[-.]?\d{4}", token):
            continue
        if re.fullmatch(r"[0-9.,-]+", token):
            continue
        if re.fullmatch(r"#?[0-9]{6,}", token):
            continue
        if re.fullmatch(r"[A-Z0-9]{5,}", token) and any(ch.isdigit() for ch in token):
            continue
        if re.fullmatch(r"[A-Z]{5,}", token) and _is_state_abbr(token[-2:]):
            continue
        if token in descriptor_tokens:
            continue
        if token in head_tokens:
            continue
        # Frequently OCR-concatenated "R" suffix on phone-like tokens.
        if token.endswith("R") and token[:-1] in head_tokens:
            continue
        return False
    return True


def _choose_amount_candidate(remainder: str) -> Optional[re.Match[str]]:
    candidates = list(AMOUNT_RE.finditer(remainder))
    if not candidates:
        return None
    valid: List[tuple[int, int, int, re.Match[str]]] = []
    for m in candidates:
        tail = remainder[m.end() :].strip()
        if not _is_redundant_tail(remainder, m):
            continue
        amount_val = _parse_amount(m.group("amount"))
        if amount_val is None:
            continue
        token = m.group("amount").strip()
        has_explicit_sign = token.startswith("-") or token.startswith("(")
        nonzero = abs(amount_val) > 0
        # Prefer explicit sign (important for ClickPay-like rows), then non-zero, then right-most.
        valid.append((1 if has_explicit_sign else 0, 1 if nonzero else 0, m.start(), m))
    if not valid:
        return None
    valid.sort(reverse=True)
    return valid[0][3]


def parse_statement_info(lines: Iterable[str]) -> StatementInfo:
    info = StatementInfo()
    period_re = re.compile(r"([A-Z][a-z]+ \d{1,2}, \d{4} to [A-Z][a-z]+ \d{1,2}, \d{4})")
    acct_re = re.compile(r"(?:account number:|account #)\s*([0-9 ]{6,})", re.IGNORECASE)
    acct_last4_re = re.compile(
        r"(?:account|card)?\s*(?:ending|ends?)\s*(?:in)?\s*[:#-]?\s*(\d{4,})\b",
        re.IGNORECASE,
    )
    owner_re = re.compile(r"^[A-Z][A-Z .'-]+$")

    if _cfg().BANK_NAME:
        info.bank_name = _cfg().BANK_NAME

    for line in lines:
        lower = line.lower()

        if info.statement_period is None:
            if "statement period" in lower:
                info.statement_period = line.split(":", 1)[-1].strip()
            else:
                match = period_re.search(line)
                if match:
                    info.statement_period = match.group(1)

        if info.account_name is None and "account name" in lower:
            info.account_name = line.split(":", 1)[-1].strip()

        if info.account_number is None:
            match = acct_re.search(line)
            if match:
                info.account_number = re.sub(r"\s+", " ", match.group(1)).strip()
            else:
                # Fallback for masked statements that expose only trailing digits
                # (e.g., "Card ending in 3384", including OCR-joined variants).
                fallback = acct_last4_re.search(line)
                if fallback:
                    info.account_number = fallback.group(1)
                else:
                    compact = re.sub(r"[^a-z0-9]", "", lower)
                    compact_match = re.search(r"(?:account|card)?(?:ending|ends?)(?:in)?(\d{4,})\b", compact)
                    if compact_match:
                        info.account_number = compact_match.group(1)

        if info.owner_name is None:
            if "account holder" in lower or "primary account" in lower:
                info.owner_name = line.split(":", 1)[-1].strip()
            elif owner_re.match(line) and not re.search(r"\d", line):
                info.owner_name = line.strip()

    return info


def is_noise(line: str) -> bool:
    lower = line.lower()
    return any(hint in lower for hint in _cfg().header_hints)


def parse_transactions(lines: Iterable[str]) -> List[Transaction]:
    transactions: List[Transaction] = []
    reconstructed_rows: List[dict] = []
    failed_rows: List[str] = []

    section: Optional[str] = None
    current_row: Optional[dict] = None

    def flush_row() -> None:
        nonlocal current_row
        if current_row:
            reconstructed_rows.append(current_row)
            current_row = None

    for line in lines:
        lower = line.lower().strip()
        anchor = DATE_ANCHOR_RE.match(line)

        # Date-anchored rows always win, even if they contain words like "payment".
        if anchor:
            flush_row()
            current_row = {"section": section, "text": line.strip()}
            continue

        matched_section = None
        for name, markers in _cfg().SECTION_HEADERS.items():
            if any(m in lower for m in markers):
                matched_section = name
                break

        if matched_section is not None:
            flush_row()
            section = matched_section
            continue

        if any(m in lower for m in _cfg().SECTION_END_MARKERS):
            flush_row()
            section = None
            continue
        if any(m in lower for m in _cfg().TABLE_HEADER_MARKERS):
            flush_row()
            continue
        if is_noise(line):
            flush_row()
            continue

        if current_row:
            current_row["text"] = f"{current_row['text']} {line.strip()}"

    flush_row()

    section_sums = {"deposits": 0.0, "withdrawals": 0.0, "fees": 0.0}

    for row in reconstructed_rows:
        text = row["text"]
        anchor = DATE_ANCHOR_RE.match(text)
        if not anchor:
            continue
        date = anchor.group("date")
        remainder = text[anchor.end() :].strip()
        remainder = _truncate_at_garbage(remainder)
        if _is_non_transaction_row(remainder):
            continue

        amount_match = _choose_amount_candidate(remainder)
        if not amount_match:
            failed_rows.append(text)
            transactions.append(
                Transaction(
                    date=date,
                    description=remainder.strip() or text.strip(),
                    amount="UNPARSED",
                    txn_type="unparsed",
                )
            )
            continue
        amount = amount_match.group("amount")
        remainder = remainder[: amount_match.start()].strip()

        remainder = re.sub(r"\s+", " ", remainder).strip(" -")

        # Bank of America sometimes splits the year as a standalone "/YY" token
        # at the start of the description. Merge it into the date.
        year_match = re.match(r"^/(\d{2})\b", remainder)
        if year_match:
            year_suffix = year_match.group(1)
            remainder = remainder[year_match.end() :].strip()
            date = f"{date}/{year_suffix}"

        if row["section"] in {"withdrawals", "fees"} and not _has_explicit_sign(amount):
            amount = f"-{amount}"
        parsed_amount = _parse_amount(amount)
        if parsed_amount is None:
            failed_rows.append(text)
            transactions.append(
                Transaction(
                    date=date,
                    description=remainder.strip() or text.strip(),
                    amount="UNPARSED",
                    txn_type="unparsed",
                )
            )
            continue

        txn_type = None
        desc_upper = remainder.upper()
        row_upper = text.upper()
        for key in _cfg().FEE_KEYWORDS:
            if key in desc_upper or key in row_upper:
                txn_type = "fee"
                break
        if txn_type is None:
            if row["section"] == "fees":
                txn_type = "fee"
            else:
                txn_type = "deposits" if parsed_amount > 0 else "withdrawals"

        transactions.append(
            Transaction(
                date=date,
                description=remainder.strip(),
                amount=amount.strip(),
                txn_type=txn_type,
            )
        )

        if txn_type == "fee":
            section_sums["fees"] += abs(parsed_amount)
        elif txn_type == "deposits":
            section_sums["deposits"] += abs(parsed_amount)
        else:
            section_sums["withdrawals"] += abs(parsed_amount)

    _set_last_parse_stats(ParseStats(
        reconstructed_rows=len(reconstructed_rows),
        output_txns=len(transactions),
        section_sums=section_sums,
        warnings=[],
        failed_rows=failed_rows,
    ))
    return transactions


def parse_account_summary(lines: Iterable[str]) -> AccountSummary:
    summary = AccountSummary()

    aliases = _cfg().SUMMARY_ALIASES

    def normalized(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    def alias_hit(line_lower: str, key: str) -> bool:
        line_norm = normalized(line_lower)
        for alias in aliases.get(key, []):
            if normalized(alias) and normalized(alias) in line_norm:
                return True
        return False

    def normalize_summary_amount(field: str, raw_amount: str) -> str:
        parsed = _parse_amount(raw_amount)
        if parsed is None:
            return raw_amount
        if field == "deposits_additions":
            return _format_money(abs(parsed))
        if field in {"withdrawals_subtractions", "service_fees", "checks"}:
            amt = abs(parsed)
            return f"-{_format_money(amt)}" if amt > 0 else _format_money(amt)
        return raw_amount

    def extract_amount(line: str) -> Optional[str]:
        matches = list(AMOUNT_RE.finditer(line))
        if not matches:
            return None
        return matches[-1].group("amount").strip()

    for line in lines:
        lower = line.lower()
        amount = extract_amount(line)
        if not amount:
            continue

        if summary.beginning_balance is None and alias_hit(lower, "beginning_balance"):
            summary.beginning_balance = normalize_summary_amount("beginning_balance", amount)
            date_match = re.search(r"\bon\b\s+(.+?)\s+\(?-?\$?\d[\d,]*\.\d{2}\)?", line, re.IGNORECASE)
            if date_match:
                summary.beginning_balance_date = date_match.group(1).strip()
            continue

        if summary.ending_balance is None and alias_hit(lower, "ending_balance"):
            summary.ending_balance = normalize_summary_amount("ending_balance", amount)
            date_match = re.search(r"\bon\b\s+(.+?)\s+\(?-?\$?\d[\d,]*\.\d{2}\)?", line, re.IGNORECASE)
            if date_match:
                summary.ending_balance_date = date_match.group(1).strip()
            continue

        if summary.deposits_additions is None and alias_hit(lower, "deposits_additions"):
            summary.deposits_additions = normalize_summary_amount("deposits_additions", amount)
            continue

        if summary.withdrawals_subtractions is None and alias_hit(lower, "withdrawals_subtractions"):
            summary.withdrawals_subtractions = normalize_summary_amount("withdrawals_subtractions", amount)
            continue

        if summary.service_fees is None and alias_hit(lower, "service_fees"):
            summary.service_fees = normalize_summary_amount("service_fees", amount)
            continue

        if summary.checks is None and alias_hit(lower, "checks"):
            summary.checks = normalize_summary_amount("checks", amount)

    return summary


def _format_money(value: float) -> str:
    return f"{value:,.2f}"


def apply_summary_fallback(summary: AccountSummary, transactions: List[Transaction]) -> AccountSummary:
    deposits = 0.0
    withdrawals = 0.0
    fees = 0.0
    for txn in transactions:
        amount = _parse_amount(txn.amount)
        if amount is None:
            continue
        txn_type = (txn.txn_type or "").lower()
        if txn_type == "fees":
            fees += abs(amount)
            continue
        if txn_type == "deposits" or amount > 0:
            deposits += abs(amount)
        elif txn_type == "withdrawals" or amount < 0:
            withdrawals += abs(amount)

    if summary.deposits_additions is None and deposits > 0:
        summary.deposits_additions = _format_money(deposits)
    if summary.withdrawals_subtractions is None and withdrawals > 0:
        summary.withdrawals_subtractions = f"-{_format_money(withdrawals)}"
    if summary.service_fees is None and fees > 0:
        summary.service_fees = f"-{_format_money(fees)}"
    return summary


def _normalize_label(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def _compact_label(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _prettify_summary_label(label: str) -> str:
    text = re.sub(r"\s+", " ", str(label or "")).strip(" :-|")
    if not text:
        return text
    if " " in text:
        return text

    # Some PDFs emit compact labels like TOTALFEESFORTHISPERIOD.
    upper = text.upper()
    tokens = [
        "MINIMUM",
        "PAYMENTS",
        "PAYMENT",
        "PURCHASES",
        "PURCHASE",
        "PREVIOUS",
        "BALANCE",
        "CREDITS",
        "CREDIT",
        "CHARGED",
        "INTEREST",
        "TOTAL",
        "FEES",
        "FEE",
        "THIS",
        "PERIOD",
        "AND",
        "FOR",
        "NEW",
        "DUE",
    ]
    token_hits = 0
    parts: List[str] = []
    i = 0
    sorted_tokens = sorted(tokens, key=len, reverse=True)
    while i < len(upper):
        match = next((tok for tok in sorted_tokens if upper.startswith(tok, i)), None)
        if match:
            parts.append(match)
            i += len(match)
            token_hits += 1
        else:
            parts.append(upper[i])
            i += 1

    rebuilt = re.sub(r"\s+", " ", " ".join(parts)).strip()
    rebuilt = re.sub(r"\b([A-Z])\s+([A-Z])\b", r"\1\2", rebuilt)
    if token_hits < 2:
        return text
    if " " in rebuilt:
        return rebuilt.title()
    return text


def _canonical_summary_key(label: str) -> Optional[str]:
    norm = _normalize_label(label)
    compact_norm = _compact_label(label)
    if not norm:
        return None
    for key, aliases in _cfg().SUMMARY_ALIASES.items():
        for alias in aliases:
            alias_norm = _normalize_label(alias)
            alias_compact = _compact_label(alias)
            if (alias_norm and alias_norm in norm) or (alias_compact and alias_compact in compact_norm):
                return key
    return None


def extract_dynamic_summary(lines: Iterable[str]) -> List[dict[str, str]]:
    cfg = _cfg()
    ignore_markers = cfg.SUMMARY_IGNORE_MARKERS
    summary_signal_terms = cfg.SUMMARY_SIGNAL_TERMS
    canonical_labels = cfg.SUMMARY_CANONICAL_LABELS
    known_credit_labels = [
        ("minimum payment due", "minimum_payment_due", "Minimum Payment Due"),
    ]
    entries: dict[str, dict[str, str]] = {}

    def extract_amount(line: str) -> Optional[str]:
        matches = list(AMOUNT_RE.finditer(line))
        if not matches:
            return None
        signed = [m for m in matches if m.group("amount").startswith(("+", "-", "("))]
        if signed:
            return signed[0].group("amount").strip()
        return matches[0].group("amount").strip()

    for raw in lines:
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        lower = line.lower()
        if any(marker in lower for marker in ignore_markers):
            continue
        if DATE_ANCHOR_RE.match(line):
            continue
        amount = extract_amount(line)
        if not amount:
            continue
        amount_idx = line.find(amount)
        if amount_idx <= 0:
            continue
        label = line[:amount_idx].strip(" :-|")
        if not label:
            continue
        if len(re.findall(r"[A-Za-z]", label)) < 3:
            continue
        if len(label) > 80:
            continue
        compact_label = _compact_label(label)
        if not any(term in compact_label for term in summary_signal_terms):
            continue
        matched_known = None
        for alias, known_key, known_display in known_credit_labels:
            if _compact_label(alias) in compact_label:
                matched_known = (known_key, known_display)
                break
        if matched_known:
            key, display = matched_known
        else:
            canon = _canonical_summary_key(label)
            if canon:
                key = canon
                display = canonical_labels.get(canon, " ".join(c.capitalize() for c in canon.split("_")))
            else:
                key = _normalize_label(label)[:64]
                display = _prettify_summary_label(label)
        if key and key not in entries:
            entries[key] = {"key": key, "label": display, "amount": amount}

        norm_line = _normalize_label(line)
        for alias, k, display_label in known_credit_labels:
            if alias in norm_line and k not in entries:
                entries[k] = {"key": k, "label": display_label, "amount": amount}
    return list(entries.values())


def infer_statement_type(dynamic_summary: List[dict[str, str]], lines: Iterable[str]) -> str:
    label_text = " ".join(item.get("label", "").lower() for item in dynamic_summary)
    page_text = " ".join(str(x).lower() for x in lines)
    text = f"{label_text} {page_text}"
    cfg = _cfg()
    credit_score = sum(1 for s in cfg.STATEMENT_TYPE_CREDIT_SIGNALS if s in text)
    debit_score = sum(1 for s in cfg.STATEMENT_TYPE_DEBIT_SIGNALS if s in text)
    if credit_score > debit_score:
        return "credit"
    if debit_score > credit_score:
        return "debit"
    return "unknown"


def build_summary_cards(
    summary: AccountSummary,
    dynamic_summary: List[dict[str, str]],
    statement_type: str,
) -> List[dict[str, str]]:
    if statement_type == "credit" and dynamic_summary:
        priority = {
            "beginning_balance": 0,
            "deposits_additions": 1,
            "withdrawals_subtractions": 2,
            "service_fees": 3,
            "ending_balance": 4,
            "minimum_payment_due": 5,
        }
        ranked = sorted(dynamic_summary, key=lambda x: priority.get(str(x.get("key")), 99))
        return ranked[:8]
    fixed_cards = [
        {"key": "beginning_balance", "label": "Beginning Balance", "amount": summary.beginning_balance or "-"},
        {"key": "deposits_additions", "label": "Income", "amount": summary.deposits_additions or "-"},
        {"key": "withdrawals_subtractions", "label": "Expenses", "amount": summary.withdrawals_subtractions or "-"},
        {"key": "service_fees", "label": "Service Fees", "amount": summary.service_fees or "-"},
        {"key": "ending_balance", "label": "Ending Balance", "amount": summary.ending_balance or "-"},
    ]
    if statement_type == "debit":
        return fixed_cards
    if dynamic_summary:
        return dynamic_summary[:8]
    return fixed_cards


def print_statement(info: StatementInfo, summary: AccountSummary, transactions: List[Transaction]) -> None:
    print("Statement Summary")
    print("-----------------")
    print(f"Bank Name: {info.bank_name or 'Unknown'}")
    if info.account_number:
        print(f"Account Number: {info.account_number}")
    if info.account_name:
        print(f"Account Name: {info.account_name}")
    if info.owner_name:
        print(f"Owner Name: {info.owner_name}")
    if info.statement_period:
        print(f"Statement Period: {info.statement_period}")
    if any(
        [
            summary.beginning_balance,
            summary.deposits_additions,
            summary.withdrawals_subtractions,
            summary.checks,
            summary.service_fees,
            summary.ending_balance,
        ]
    ):
        print("")
        print("Account Summary")
        print("---------------")
        if summary.beginning_balance:
            if summary.beginning_balance_date:
                print(f"Beginning Balance ({summary.beginning_balance_date}): {summary.beginning_balance}")
            else:
                print(f"Beginning Balance: {summary.beginning_balance}")
        if summary.deposits_additions:
            print(f"Deposits and Other Additions: {summary.deposits_additions}")
        if summary.withdrawals_subtractions:
            print(f"Withdrawals and Other Subtractions: {summary.withdrawals_subtractions}")
        if summary.checks:
            print(f"Checks: {summary.checks}")
        if summary.service_fees:
            print(f"Service Fees: {summary.service_fees}")
        if summary.ending_balance:
            if summary.ending_balance_date:
                print(f"Ending Balance ({summary.ending_balance_date}): {summary.ending_balance}")
            else:
                print(f"Ending Balance: {summary.ending_balance}")
    print("")
    print("Transactions")
    print("------------")
    stats = _last_parse_stats()
    if stats:
        print(f"DEBUG: Reconstructed rows: {stats.reconstructed_rows}")
        print(f"DEBUG: Parsed transactions: {stats.output_txns}")
        if stats.reconstructed_rows != stats.output_txns:
            print(
                f"WARNING: Reconstructed {stats.reconstructed_rows} rows "
                f"but output {stats.output_txns} transactions."
            )
        failed = stats.failed_rows or []
        print(f"DEBUG: Failed rows: {len(failed)}")
        if failed:
            print("DEBUG: First 20 failed rows:")
            for row in failed[:20]:
                print(f"  - {row}")
    for txn in transactions:
        cleaned = merchant_clean(txn.description)
        print(f"{txn.date} | {cleaned} | {txn.amount}")

    if stats and stats.section_sums:
        warnings = []
        tol = 0.01
        if summary.deposits_additions:
            target = _parse_amount(str(summary.deposits_additions))
            if target is not None:
                delta = abs(stats.section_sums["deposits"] - abs(target))
                if delta > tol:
                    warnings.append(
                        f"Deposits mismatch: parsed {stats.section_sums['deposits']:.2f} vs "
                        f"statement {abs(target):.2f}"
                    )
        if summary.withdrawals_subtractions:
            target = _parse_amount(str(summary.withdrawals_subtractions))
            if target is not None:
                delta = abs(stats.section_sums["withdrawals"] - abs(target))
                if delta > tol:
                    warnings.append(
                        f"Withdrawals mismatch: parsed {stats.section_sums['withdrawals']:.2f} vs "
                        f"statement {abs(target):.2f}"
                    )
        if summary.service_fees:
            target = _parse_amount(str(summary.service_fees))
            if target is not None:
                delta = abs(stats.section_sums["fees"] - abs(target))
                if delta > tol:
                    warnings.append(
                        f"Fees mismatch: parsed {stats.section_sums['fees']:.2f} vs "
                        f"statement {abs(target):.2f}"
                    )
        for warning in warnings:
            print(f"WARNING: {warning}")

# Pipeline helper: resolve bank config path with explicit > detected > inferred > fallback precedence.
def _resolve_parser_config_path(lines: List[str], explicit_config_path: Optional[Path]) -> Path:
    if explicit_config_path is not None:
        return explicit_config_path
    config_dir = PROJECT_ROOT / "config" / "banks"
    auto_path = _detect_config_path(lines, config_dir)
    if auto_path is not None:
        return auto_path
    inferred_path = _infer_config_path_by_bank_name(lines, config_dir)
    if inferred_path is not None:
        return inferred_path
    # Very last resort only when no known bank config can be resolved.
    return config_dir / "unknown_config.json"


# Pipeline helper: generic parse stage that must work even without bank-specific refinement.
def _parse_generic_stage(lines: List[str]) -> tuple[StatementInfo, List[Transaction], AccountSummary]:
    info = parse_statement_info(lines)
    transactions = parse_transactions(lines)
    summary = apply_summary_fallback(parse_account_summary(lines), transactions)
    return info, transactions, summary


# Pipeline helper: derive statement-level context used by UI cards and sign normalization.
def _infer_statement_context(lines: List[str], summary: AccountSummary) -> tuple[dict[str, str], str, List[dict[str, str]]]:
    dynamic_summary = extract_dynamic_summary(lines)
    statement_type = infer_statement_type(dynamic_summary, lines)
    summary_cards = build_summary_cards(summary, dynamic_summary, statement_type)
    return dynamic_summary, statement_type, summary_cards


def _normalize_txn_type_for_statement(
    txn_type: Optional[str], amount: str, statement_type: str, raw_description: str
) -> Optional[str]:
    base = (txn_type or "").lower().strip()
    if statement_type != "credit":
        return txn_type
    if base in {"fee", "unparsed"}:
        return txn_type

    # For credit-card statements, purchases are usually positive amounts and
    # credits/payments/refunds are negative amounts.
    parsed = _parse_amount(amount)
    if parsed is None:
        return txn_type
    desc_upper = raw_description.upper()
    credit_hints = (
        "PAYMENT",
        "PAYMENTS",
        "CREDIT",
        "REFUND",
        "REVERSAL",
        "REWARD",
        "CASHBACK",
    )
    if any(hint in desc_upper for hint in credit_hints):
        return "deposits"
    return "withdrawals" if parsed > 0 else "deposits"


# Pipeline helper: normalize transaction-facing fields after parsing/refinement.
def _build_normalized_transactions(transactions: List[Transaction], statement_type: str) -> List[dict[str, Optional[str]]]:
    out: List[dict[str, Optional[str]]] = []
    for txn in transactions:
        merchant = merchant_clean(txn.description)
        merchant = _apply_venmo_direction(merchant, txn.description, txn.txn_type)
        normalized_txn_type = _normalize_txn_type_for_statement(
            txn.txn_type, txn.amount, statement_type, txn.description
        )
        out.append(
            {
                "date": txn.date,
                "raw_description": txn.description,
                "merchant_clean": merchant,
                "amount": txn.amount,
                "txn_type": normalized_txn_type,
            }
        )
    return out


# Pipeline helper: convert thread-local parse stats into API payload shape.
def _build_parse_stats_payload() -> dict:
    stats = _last_parse_stats()
    return {
        "reconstructed_rows": stats.reconstructed_rows if stats else 0,
        "parsed_transactions": stats.output_txns if stats else 0,
        "failed_rows": stats.failed_rows if stats and stats.failed_rows else [],
        "section_sums": stats.section_sums if stats and stats.section_sums else {},
    }


def parse_pdf_to_payload(pdf_path: Path, config_path: Optional[Path] = None) -> dict:
    # Orchestrator: extract text -> load config -> generic parse -> context -> payload shaping.
    lines = extract_text(pdf_path)
    resolved_config_path = _resolve_parser_config_path(lines, config_path)
    _cfg().load_from_json(resolved_config_path)
    _merch_cfg().load_from_json()

    info, transactions, summary = _parse_generic_stage(lines)
    dynamic_summary, statement_type, summary_cards = _infer_statement_context(lines, summary)
    tx_payload = _build_normalized_transactions(transactions, statement_type)
    parse_stats = _build_parse_stats_payload()

    return {
        "statement_type": statement_type,
        "statement_info": {
            "bank_name": info.bank_name,
            "account_name": info.account_name,
            "owner_name": info.owner_name,
            "statement_period": info.statement_period,
            "account_number": info.account_number,
        },
        "account_summary": {
            "beginning_balance": summary.beginning_balance,
            "beginning_balance_date": summary.beginning_balance_date,
            "deposits_additions": summary.deposits_additions,
            "withdrawals_subtractions": summary.withdrawals_subtractions,
            "checks": summary.checks,
            "service_fees": summary.service_fees,
            "ending_balance": summary.ending_balance,
            "ending_balance_date": summary.ending_balance_date,
        },
        "account_summary_dynamic": dynamic_summary,
        "summary_cards": summary_cards,
        "transactions": tx_payload,
        "parse_stats": parse_stats,
    }


def main() -> int:
    config_path = None
    if "--config" in sys.argv:
        idx = sys.argv.index("--config")
        if idx + 1 < len(sys.argv):
            config_path = Path(sys.argv[idx + 1]).expanduser()
            # Remove config args before normal arg parsing.
            del sys.argv[idx : idx + 2]
    if len(sys.argv) < 2:
        print("Usage: python3 parse_bank.py <statement.pdf> [--config <config.json>]")
        return 1
    pdf_path = Path(sys.argv[1]).expanduser()
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}")
        return 1

    payload = parse_pdf_to_payload(pdf_path, config_path)
    info = StatementInfo(**payload["statement_info"])
    summary = AccountSummary(**payload["account_summary"])
    transactions = [
        Transaction(
            date=t["date"],
            description=t["raw_description"],
            amount=t["amount"],
            txn_type=t.get("txn_type"),
        )
        for t in payload["transactions"]
    ]
    print_statement(info, summary, transactions)
    csv_path = export_transactions_csv(payload, pdf_path)
    print("")
    print(f"CSV written: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
