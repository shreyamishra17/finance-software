#!/usr/bin/env python3
from __future__ import annotations

import json
import csv
import os
import re
import sys
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
    CONFIG_PATH = PROJECT_ROOT / "config" / "boa_config.json"

    BOILERPLATE_PATTERNS: List[str] = []
    PHONE_RE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")
    MMDD_RE = re.compile(r"\b(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\b")
    MM_DD_RE = re.compile(r"\b(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])\b")
    LONG_ID_RE = re.compile(r"\b[0-9]{8,}\b")

    STATE_ABBRS: set[str] = set()
    MARKERS: dict = {}
    LOCATION_WORDS: set[str] = set()
    LOCATION_PREFIX_WORDS: set[str] = set()
    LOCATION_PHRASES: List[str] = []
    STOP_PHRASES: List[str] = []
    FEE_KEYWORDS: List[str] = []
    FEE_LABELS: dict = {}
    SECTION_HEADERS: dict = {}
    SECTION_END_MARKERS: List[str] = []
    TABLE_HEADER_MARKERS: List[str] = []
    MERCHANT_MARKERS: List[dict] = []
    P2P_RULES: List[dict] = []
    BANK_NAME: Optional[str] = None
    BANK_ALIASES: List[str] = []
    IGNORABLE_TAIL_PATTERNS: List[str] = []
    METADATA_LABELS: List[str] = ["DES", "ID", "INDN", "CO ID", "PPD", "WEB", "INFO", "PMT"]
    PREFERRED_MERCHANT_LABELS: List[str] = ["DES"]

    @classmethod
    def load_from_json(cls, config_path: Optional[Path] = None) -> None:
        if config_path is not None:
            cls.CONFIG_PATH = config_path
        if not cls.CONFIG_PATH.exists():
            return
        try:
            with cls.CONFIG_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return

        markers = data.get("markers", {})
        if isinstance(markers, dict):
            cls.MARKERS = {str(k): v for k, v in markers.items()}

        stop_phrases = data.get("stop_phrases", [])
        if isinstance(stop_phrases, list):
            cls.STOP_PHRASES = [str(x).lower() for x in stop_phrases if str(x).strip()]

        fee_keywords = data.get("fee_keywords", [])
        if isinstance(fee_keywords, list):
            cls.FEE_KEYWORDS = [str(x).upper() for x in fee_keywords if str(x).strip()]

        fee_labels = data.get("fee_labels", {})
        if isinstance(fee_labels, dict):
            cls.FEE_LABELS = {str(k).upper(): str(v) for k, v in fee_labels.items() if str(k).strip()}

        boilerplate = data.get("boilerplate_patterns", [])
        if isinstance(boilerplate, list):
            cls.BOILERPLATE_PATTERNS = [str(x) for x in boilerplate if str(x).strip()]

        state_abbrs = data.get("state_abbrs", [])
        if isinstance(state_abbrs, list):
            cls.STATE_ABBRS = {str(x).upper() for x in state_abbrs if str(x).strip()}

        locations = data.get("locations", [])
        if isinstance(locations, list):
            normalized = [str(x).strip() for x in locations if str(x).strip()]
            cls.LOCATION_PHRASES = normalized
            words = set()
            prefix_words = set()
            for phrase in normalized:
                parts = phrase.lower().split()
                for part in parts:
                    words.add(part)
                if len(parts) > 1:
                    prefix_words.update(parts[:-1])
            cls.LOCATION_WORDS = words
            cls.LOCATION_PREFIX_WORDS = prefix_words

        header_hints = data.get("header_hints", [])
        if isinstance(header_hints, list):
            global HEADER_HINTS
            HEADER_HINTS = [str(x).lower() for x in header_hints if str(x).strip()]

        section_headers = data.get("section_headers", {})
        if isinstance(section_headers, dict):
            cls.SECTION_HEADERS = {
                str(k): [str(x).lower() for x in v if str(x).strip()]
                for k, v in section_headers.items()
                if isinstance(v, list)
            }

        section_end = data.get("section_end_markers", [])
        if isinstance(section_end, list):
            cls.SECTION_END_MARKERS = [str(x).lower() for x in section_end if str(x).strip()]

        table_headers = data.get("table_header_markers", [])
        if isinstance(table_headers, list):
            cls.TABLE_HEADER_MARKERS = [str(x).lower() for x in table_headers if str(x).strip()]

        merchant_markers = data.get("merchant_markers", [])
        if isinstance(merchant_markers, list):
            cls.MERCHANT_MARKERS = [m for m in merchant_markers if isinstance(m, dict)]

        p2p_rules = data.get("p2p_rules", [])
        if isinstance(p2p_rules, list):
            cls.P2P_RULES = [r for r in p2p_rules if isinstance(r, dict)]

        bank_name = data.get("bank_name")
        if isinstance(bank_name, str) and bank_name.strip():
            cls.BANK_NAME = bank_name.strip()

        bank_aliases = data.get("bank_aliases", [])
        if isinstance(bank_aliases, list):
            cls.BANK_ALIASES = [str(x).lower() for x in bank_aliases if str(x).strip()]

        ignorable_tail = data.get("ignorable_tail_patterns", [])
        if isinstance(ignorable_tail, list):
            cls.IGNORABLE_TAIL_PATTERNS = [str(x) for x in ignorable_tail if str(x).strip()]

        metadata_labels = data.get("metadata_labels", [])
        if isinstance(metadata_labels, list):
            parsed = [str(x).strip() for x in metadata_labels if str(x).strip()]
            if parsed:
                cls.METADATA_LABELS = parsed

        preferred_labels = data.get("preferred_merchant_labels", [])
        if isinstance(preferred_labels, list):
            parsed = [str(x).strip() for x in preferred_labels if str(x).strip()]
            if parsed:
                cls.PREFERRED_MERCHANT_LABELS = parsed


class MerchantNormalizationConfig:
    CONFIG_PATH = PROJECT_ROOT / "config" / "merchant_normalization.json"

    TOKEN_REPLACEMENTS: dict[str, str] = {}
    ALIASES: dict[str, str] = {}
    CONTAINS_ALIASES: dict[str, str] = {}
    STRIP_SUFFIX_TOKENS: set[str] = set()

    @classmethod
    def load_from_json(cls, config_path: Optional[Path] = None) -> None:
        path = config_path or cls.CONFIG_PATH
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return

        token_replacements = data.get("token_replacements", {})
        if isinstance(token_replacements, dict):
            cls.TOKEN_REPLACEMENTS = {
                str(k).upper().strip(): str(v).strip()
                for k, v in token_replacements.items()
                if str(k).strip() and str(v).strip()
            }

        aliases = data.get("aliases", {})
        if isinstance(aliases, dict):
            cls.ALIASES = {
                str(k).upper().strip(): str(v).strip()
                for k, v in aliases.items()
                if str(k).strip() and str(v).strip()
            }

        contains_aliases = data.get("contains_aliases", {})
        if isinstance(contains_aliases, dict):
            cls.CONTAINS_ALIASES = {
                str(k).upper().strip(): str(v).strip()
                for k, v in contains_aliases.items()
                if str(k).strip() and str(v).strip()
            }

        strip_suffix_tokens = data.get("strip_suffix_tokens", [])
        if isinstance(strip_suffix_tokens, list):
            cls.STRIP_SUFFIX_TOKENS = {str(x).upper().strip() for x in strip_suffix_tokens if str(x).strip()}


HEADER_HINTS: List[str] = []


def _strip_token_punct(token: str) -> str:
    # Strip common punctuation, including asterisks used in statement markers.
    return token.strip(" ,;:*()[]{}<>")


def _is_state_abbr(token: str) -> bool:
    return token.isalpha() and token.isupper() and token in ParserConfig.STATE_ABBRS


def safe_cleanup(raw: str) -> str:
    text = raw.strip()

    # Remove boilerplate phrases conservatively.
    for pattern in ParserConfig.BOILERPLATE_PATTERNS:
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
    for phrase in ParserConfig.STOP_PHRASES:
        idx = lower.find(phrase)
        if idx != -1:
            cut = idx if cut is None else min(cut, idx)
    return text[:cut].strip() if cut is not None else text


def _stage_a_truncate(raw: str) -> str:
    return _truncate_at_garbage(raw)


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

    for rule in ParserConfig.MERCHANT_MARKERS:
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
    for rule in ParserConfig.P2P_RULES:
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


def _strip_location_tail(tokens: List[str]) -> List[str]:
    if not tokens:
        return tokens

    original = list(tokens)
    lowered = [t.lower() for t in tokens]

    phrases = sorted(
        ParserConfig.LOCATION_PHRASES,
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

    if tokens and tokens[-1].lower() in ParserConfig.LOCATION_WORDS:
        tokens = tokens[:-1]

    # Remove orphan location prefix tokens left at the end
    # (e.g., "NEW" after stripping "YORK"/state from "NEW YORK ...").
    while len(tokens) > 1 and tokens[-1].lower() in ParserConfig.LOCATION_PREFIX_WORDS:
        tokens = tokens[:-1]

    return tokens if tokens else original


def _strip_state_zip_anchor(tokens: List[str]) -> List[str]:
    # Strip everything from the last state abbreviation to the end.
    for i in range(len(tokens) - 1, -1, -1):
        if _is_state_abbr(tokens[i].upper()):
            return tokens[:i]
    return tokens


def _normalize_alias_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", text.upper())).strip()


def _strip_location_like_merchant_tail(parts: List[str]) -> List[str]:
    if not parts:
        return parts

    compact_locations = {
        re.sub(r"[^A-Za-z0-9]", "", phrase).upper()
        for phrase in ParserConfig.LOCATION_PHRASES
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
        for st in ParserConfig.STATE_ABBRS:
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
    while parts and parts[-1].upper() in MerchantNormalizationConfig.STRIP_SUFFIX_TOKENS:
        parts.pop()

    # Remove compact location-like tails (e.g., CALaJolla) using configured locations.
    parts = _strip_location_like_merchant_tail(parts)

    # Token-level replacements.
    replaced_parts: List[str] = []
    for part in parts:
        replacement = MerchantNormalizationConfig.TOKEN_REPLACEMENTS.get(part.upper())
        if replacement:
            replaced_parts.extend(replacement.split())
        else:
            replaced_parts.append(part)

    normalized_text = " ".join(replaced_parts).strip()
    alias_key = _normalize_alias_key(normalized_text)

    if alias_key in MerchantNormalizationConfig.ALIASES:
        return MerchantNormalizationConfig.ALIASES[alias_key]

    for key, canonical in MerchantNormalizationConfig.CONTAINS_ALIASES.items():
        if key and key in alias_key:
            return canonical

    return re.sub(r"\s+", " ", normalized_text).strip()


def _is_good_merchant_candidate(text: str) -> bool:
    if len(text.strip()) < 3:
        return False
    words = re.findall(r"[A-Za-z]+", text)
    if len(words) >= 2:
        return True
    return len(words) == 1 and len(words[0]) >= 4


def _extract_structured_merchant(raw: str) -> Optional[str]:
    labels = [label.strip() for label in ParserConfig.METADATA_LABELS if label.strip()]
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

    for preferred in ParserConfig.PREFERRED_MERCHANT_LABELS:
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
    for key in ParserConfig.FEE_KEYWORDS:
        if key in upper:
            label = ParserConfig.FEE_LABELS.get(key, key.title())
            return label, "fee"

    stage = name.strip()
    is_p2p_formatted = False
    for rule in ParserConfig.P2P_RULES:
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

    if not is_p2p_formatted:
        tokens = _strip_state_zip_anchor(tokens)
        tokens = _strip_location_tail(tokens)
    tokens = _fuzzy_dedupe(tokens)

    cleaned = " ".join(tokens).strip()
    if len(cleaned) < 3:
        cleaned = fallback.strip()
    return cleaned, None


def merchant_clean(raw: str) -> str:
    stage_a = _stage_a_truncate(raw)
    structured = _extract_structured_merchant(stage_a)
    stage_b = _stage_b_extract(structured or stage_a)
    cleaned = safe_cleanup(stage_b)
    refined, _ = _refine_merchant(cleaned, structured or stage_b or stage_a)
    normalized = _apply_merchant_normalization(refined)
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
    channel_rules = [
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
    for needle, label in channel_rules:
        if needle in text:
            return label
    return "NA"


def _extract_transaction_code(raw_description: str) -> str:
    text = raw_description.upper()
    code_patterns = [
        r"\bSQ\b",
        r"\bTST\*?\b",
        r"\bDES:?\b",
        r"\bID:?\b",
        r"\bACH\b",
    ]
    for pattern in code_patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0).replace(":", "").replace("*", "")
    return "NA"


def _extract_city_state_zip(raw_description: str) -> tuple[str, str, str]:
    text = re.sub(r"\s+", " ", raw_description).strip()
    if not text:
        return "NA", "NA", "NA"

    zip_code = "NA"
    state = "NA"
    city = "NA"

    # Prefer right-most known location phrase that leaves only optional state/zip at tail.
    lowered = text.lower()
    best_match: tuple[int, int, str, str, str] | None = None  # (start_idx, length, city, state, zip)
    for phrase in ParserConfig.LOCATION_PHRASES:
        p = phrase.strip()
        if not p:
            continue
        phrase_re = re.compile(rf"\b{re.escape(p)}\b", flags=re.IGNORECASE)
        for m in phrase_re.finditer(text):
            tail = text[m.end() :].strip()
            tail_match = re.fullmatch(r"(?:(?P<state>[A-Z]{2}))?(?:\s+(?P<zip>\d{5}(?:-\d{4})?))?\s*", tail)
            if not tail_match:
                continue
            candidate_state = (tail_match.group("state") or "").upper()
            candidate_zip = tail_match.group("zip") or "NA"
            if candidate_state and candidate_state not in ParserConfig.STATE_ABBRS:
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
    if work and work[-1].upper() in ParserConfig.STATE_ABBRS:
        tail_state = work[-1].upper()
        work = work[:-1]
    compact_city_tail = re.sub(r"[^A-Za-z0-9]", "", "".join(work[-3:]).upper()) if work else ""
    if compact_city_tail:
        for phrase in sorted(ParserConfig.LOCATION_PHRASES, key=len, reverse=True):
            p = phrase.strip()
            if not p:
                continue
            compact_phrase = re.sub(r"[^A-Za-z0-9]", "", p).upper()
            if compact_phrase and compact_city_tail.endswith(compact_phrase):
                return p, tail_state, tail_zip

    # Tail match only: CITY STATE [ZIP] at end of description.
    tail_match = re.search(
        r"\b(?P<city>[A-Za-z][A-Za-z .'-]{1,40}?)\s+(?P<state>[A-Z]{2})(?:\s+(?P<zip>\d{5}(?:-\d{4})?))?\s*$",
        text,
    )
    if tail_match:
        candidate_state = tail_match.group("state").upper()
        if candidate_state in ParserConfig.STATE_ABBRS:
            state = candidate_state
            city = re.sub(r"\s+", " ", tail_match.group("city")).strip(" ,.-")
            zip_group = tail_match.group("zip")
            zip_code = zip_group if zip_group else "NA"
            city_tokens = [tok for tok in city.split() if tok]
            city_is_known_single = len(city_tokens) == 1 and city_tokens[0].lower() in ParserConfig.LOCATION_WORDS
            if len(city_tokens) < 2 and not city_is_known_single and zip_code == "NA":
                return "NA", "NA", "NA"
            return (city if city else "NA"), state, zip_code

    # Fallback: known location phrase at end (no state/zip present).
    for phrase in sorted(ParserConfig.LOCATION_PHRASES, key=len, reverse=True):
        p = phrase.strip()
        if p and lowered.endswith(p.lower()):
            return p, "NA", "NA"

    # Last-token merge fallback (e.g. "... LA JOLLA" as separate words at tail).
    tokens = re.findall(r"[A-Za-z0-9]+", text.upper())
    tail_window = tokens[-4:] if len(tokens) >= 4 else tokens
    for n in (3, 2):
        if len(tail_window) < n:
            continue
        merged = "".join(tail_window[-n:])
        for phrase in ParserConfig.LOCATION_PHRASES:
            compact_phrase = re.sub(r"[^A-Za-z0-9]", "", phrase).upper()
            if compact_phrase and merged == compact_phrase:
                return phrase, "NA", "NA"

    # Fallback: trailing standalone state code (city unknown).
    tail_state_only = re.search(r"\b([A-Z]{2})\b\s*$", text.upper())
    if tail_state_only:
        candidate_state = tail_state_only.group(1)
        if candidate_state in ParserConfig.STATE_ABBRS:
            return "NA", candidate_state, "NA"

    # Fallback: state code merged into domain-like token suffix (e.g. .COMCA).
    domainish_tokens = re.findall(r"[A-Za-z0-9./-]+", text)
    for token in reversed(domainish_tokens):
        upper_tok = token.upper()
        suffix = upper_tok[-2:]
        if suffix not in ParserConfig.STATE_ABBRS:
            continue
        prefix = upper_tok[:-2]
        if not prefix:
            continue
        if (("." in prefix) or ("/" in prefix)) and re.search(r"[A-Z]", prefix):
            return "NA", suffix, "NA"

    return "NA", "NA", "NA"


def _extract_reference_id(raw_description: str) -> str:
    text = raw_description
    phone_re = re.compile(r"^\+?\d{1,3}[-.\s]?\d{3}[-.\s]?\d{3}[-.\s]?\d{4}$")

    def is_phone_like(value: str) -> bool:
        token = value.strip()
        if phone_re.fullmatch(token):
            return True
        digits_only = re.sub(r"\D", "", token)
        return len(digits_only) == 10 and token != digits_only

    # Prefer first transaction-level ID and ignore company-level "CO ID".
    id_pattern = re.compile(r"\b(?:CO\s+)?ID:?\s*([A-Z0-9-]{4,})\b", flags=re.IGNORECASE)
    for match in id_pattern.finditer(text):
        full = match.group(0)
        if re.match(r"^\s*CO\s+ID:?", full, flags=re.IGNORECASE):
            continue
        candidate = match.group(1)
        if not is_phone_like(candidate):
            return candidate

    conf_match = re.search(r"\bCONF#?\s*([A-Z0-9-]{4,})\b", text, flags=re.IGNORECASE)
    if conf_match:
        candidate = conf_match.group(1)
        if not is_phone_like(candidate):
            return candidate

    # Company identifiers are metadata, not transaction-level references.
    text_no_company_ids = re.sub(r"\bCO\s+ID:?\s*[A-Z0-9-]{4,}\b", " ", text, flags=re.IGNORECASE)

    # Fallback to ID-like tokens only (must contain digits), avoiding plain words.
    tokens = re.findall(r"\b[A-Z0-9-]{8,}\b", text_no_company_ids.upper())
    for token in tokens:
        digit_count = sum(ch.isdigit() for ch in token)
        if digit_count >= 2 and not is_phone_like(token):
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

    city_last = norm(city_tokens[-1])
    if not city_last:
        return merchant

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
        # Handles compact prefixes like MAD -> MADRID, NEW -> NEWYORK tail token.
        if len(tail) >= 3 and city_last.startswith(tail):
            merchant_tokens.pop()
            changed = True
            continue
        break

    cleaned = " ".join(merchant_tokens).strip()
    return cleaned if (changed and cleaned) else merchant


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


def _build_csv_rows(payload: dict, source_file: str) -> List[dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    rows: List[dict[str, str]] = []
    for txn in payload.get("transactions", []):
        date = str(txn.get("date") or "NA")
        raw_description = str(txn.get("raw_description") or "")
        merchant = str(txn.get("merchant_clean") or "NA")
        amount = str(txn.get("amount") or "NA")

        dedupe_key = (date, raw_description, amount)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        city, state, zip_code = _extract_city_state_zip(raw_description)
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
                "reference_id": _extract_reference_id(raw_description),
                "source_file": source_file if source_file.strip() else "NA",
            }
        )
    return rows


def _write_csv_rows(csv_path: Path, rows: List[dict[str, str]], append: bool) -> None:
    existing_keys: set[tuple[str, str, str, str]] = set()
    if append and csv_path.exists():
        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                existing_keys.add(
                    (
                        row.get("date", "NA"),
                        row.get("merchant_raw", "NA"),
                        row.get("amount", "NA"),
                        row.get("reference_id", "NA"),
                    )
                )

    mode = "a" if append and csv_path.exists() else "w"
    with csv_path.open(mode, encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        if mode == "w":
            writer.writeheader()
        for row in rows:
            row_clean = {k: (v if str(v).strip() else "NA") for k, v in row.items()}
            key = (
                row_clean["date"],
                row_clean["merchant_raw"],
                row_clean["amount"],
                row_clean["reference_id"],
            )
            if key in existing_keys:
                continue
            writer.writerow(row_clean)
            existing_keys.add(key)


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

    period = statement.get("statement_period")
    period_slug = _statement_range_slug(period, Path(input_name))
    base_pdf_name = f"{period_slug}_{account_slug}_{_sanitize_slug(Path(input_name).stem, 'statement')}.pdf"
    saved_pdf_path = user_raw_dir / base_pdf_name
    if saved_pdf_path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        saved_pdf_path = user_raw_dir / f"{period_slug}_{account_slug}_{_sanitize_slug(Path(input_name).stem, 'statement')}_{stamp}.pdf"
    saved_pdf_path.write_bytes(pdf_bytes)

    account_csv_name = f"acc_{account_slug}.csv"
    account_csv_path = user_processed_dir / account_csv_name
    rows = _build_csv_rows(payload, saved_pdf_path.name)
    _write_csv_rows(account_csv_path, rows, append=True)

    return {
        "saved_pdf_path": str(saved_pdf_path),
        "account_csv_path": str(account_csv_path),
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


LAST_PARSE_STATS: Optional[ParseStats] = None


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
    # Match bank aliases from any config JSON in the same directory.
    text = " ".join(lines).lower()
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
            if alias_str and alias_str in text:
                return cfg_path
    return None


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
    for key in ParserConfig.FEE_KEYWORDS:
        stripped = re.sub(re.escape(key), " ", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if not stripped:
        return True
    tokens = stripped.split()
    for token in tokens:
        ok = any(re.fullmatch(p, token, flags=re.IGNORECASE) for p in ParserConfig.IGNORABLE_TAIL_PATTERNS)
        if not ok and _is_state_abbr(token.upper()):
            ok = True
        if not ok:
            return False
    return True


def _choose_amount_candidate(remainder: str) -> Optional[re.Match[str]]:
    candidates = list(AMOUNT_RE.finditer(remainder))
    if not candidates:
        return None
    valid: List[tuple[int, int, int, re.Match[str]]] = []
    for m in candidates:
        tail = remainder[m.end() :].strip()
        if not _is_ignorable_tail(tail):
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
    owner_re = re.compile(r"^[A-Z][A-Z .'-]+$")

    if ParserConfig.BANK_NAME:
        info.bank_name = ParserConfig.BANK_NAME

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

        if info.owner_name is None:
            if "account holder" in lower or "primary account" in lower:
                info.owner_name = line.split(":", 1)[-1].strip()
            elif owner_re.match(line) and not re.search(r"\d", line):
                info.owner_name = line.strip()

    return info


def is_noise(line: str) -> bool:
    lower = line.lower()
    return any(hint in lower for hint in HEADER_HINTS)


def parse_transactions(lines: Iterable[str]) -> List[Transaction]:
    global LAST_PARSE_STATS
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
        for name, markers in ParserConfig.SECTION_HEADERS.items():
            if any(m in lower for m in markers):
                matched_section = name
                break

        if matched_section is not None:
            flush_row()
            section = matched_section
            continue

        if any(m in lower for m in ParserConfig.SECTION_END_MARKERS):
            flush_row()
            section = None
            continue
        if any(m in lower for m in ParserConfig.TABLE_HEADER_MARKERS):
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
        remainder = _stage_a_truncate(remainder)

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
        for key in ParserConfig.FEE_KEYWORDS:
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

    LAST_PARSE_STATS = ParseStats(
        reconstructed_rows=len(reconstructed_rows),
        output_txns=len(transactions),
        section_sums=section_sums,
        warnings=[],
        failed_rows=failed_rows,
    )
    return transactions


def parse_account_summary(lines: Iterable[str]) -> AccountSummary:
    summary = AccountSummary()
    in_summary = False

    for line in lines:
        lower = line.lower()

        if lower.startswith("account summary"):
            in_summary = True
            continue

        if in_summary and ("important information" in lower or "date description amount" in lower):
            break

        if not in_summary:
            continue

        if "beginning balance on" in lower:
            summary.beginning_balance = line.split()[-1]
            match = re.search(r"beginning balance on (.+?)\s+\$?[-0-9,]+\.\d{2}", line, re.IGNORECASE)
            if match:
                summary.beginning_balance_date = match.group(1).strip()
        elif "deposits and other additions" in lower:
            summary.deposits_additions = line.split()[-1]
        elif "withdrawals and other subtractions" in lower:
            summary.withdrawals_subtractions = line.split()[-1]
        elif lower.startswith("checks"):
            summary.checks = line.split()[-1]
        elif lower.startswith("service fees"):
            summary.service_fees = line.split()[-1]
        elif "ending balance on" in lower:
            summary.ending_balance = line.split()[-1]
            match = re.search(r"ending balance on (.+?)\s+\$?[-0-9,]+\.\d{2}", line, re.IGNORECASE)
            if match:
                summary.ending_balance_date = match.group(1).strip()

    return summary


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
    if LAST_PARSE_STATS:
        print(f"DEBUG: Reconstructed rows: {LAST_PARSE_STATS.reconstructed_rows}")
        print(f"DEBUG: Parsed transactions: {LAST_PARSE_STATS.output_txns}")
        if LAST_PARSE_STATS.reconstructed_rows != LAST_PARSE_STATS.output_txns:
            print(
                f"WARNING: Reconstructed {LAST_PARSE_STATS.reconstructed_rows} rows "
                f"but output {LAST_PARSE_STATS.output_txns} transactions."
            )
        failed = LAST_PARSE_STATS.failed_rows or []
        print(f"DEBUG: Failed rows: {len(failed)}")
        if failed:
            print("DEBUG: First 20 failed rows:")
            for row in failed[:20]:
                print(f"  - {row}")
    for txn in transactions:
        cleaned = merchant_clean(txn.description)
        print(f"{txn.date} | {cleaned} | {txn.amount}")

    if LAST_PARSE_STATS and LAST_PARSE_STATS.section_sums:
        warnings = []
        tol = 0.01
        if summary.deposits_additions:
            target = _parse_amount(str(summary.deposits_additions))
            if target is not None:
                delta = abs(LAST_PARSE_STATS.section_sums["deposits"] - abs(target))
                if delta > tol:
                    warnings.append(
                        f"Deposits mismatch: parsed {LAST_PARSE_STATS.section_sums['deposits']:.2f} vs "
                        f"statement {abs(target):.2f}"
                    )
        if summary.withdrawals_subtractions:
            target = _parse_amount(str(summary.withdrawals_subtractions))
            if target is not None:
                delta = abs(LAST_PARSE_STATS.section_sums["withdrawals"] - abs(target))
                if delta > tol:
                    warnings.append(
                        f"Withdrawals mismatch: parsed {LAST_PARSE_STATS.section_sums['withdrawals']:.2f} vs "
                        f"statement {abs(target):.2f}"
                    )
        if summary.service_fees:
            target = _parse_amount(str(summary.service_fees))
            if target is not None:
                delta = abs(LAST_PARSE_STATS.section_sums["fees"] - abs(target))
                if delta > tol:
                    warnings.append(
                        f"Fees mismatch: parsed {LAST_PARSE_STATS.section_sums['fees']:.2f} vs "
                        f"statement {abs(target):.2f}"
                    )
        for warning in warnings:
            print(f"WARNING: {warning}")


DEFAULT_PDF_PATH = PROJECT_ROOT / "my_finance" / "raw" / "Apr10-25.pdf"


def parse_pdf_to_payload(pdf_path: Path, config_path: Optional[Path] = None) -> dict:
    lines = extract_text(pdf_path)
    if config_path is None:
        auto_path = _detect_config_path(lines, PROJECT_ROOT / "config")
        if auto_path is not None:
            config_path = auto_path
    ParserConfig.load_from_json(config_path)
    MerchantNormalizationConfig.load_from_json()

    info = parse_statement_info(lines)
    summary = parse_account_summary(lines)
    transactions = parse_transactions(lines)

    tx_payload = []
    for txn in transactions:
        merchant = merchant_clean(txn.description)
        merchant = _apply_venmo_direction(merchant, txn.description, txn.txn_type)
        tx_payload.append(
            {
                "date": txn.date,
                "raw_description": txn.description,
                "merchant_clean": merchant,
                "amount": txn.amount,
                "txn_type": txn.txn_type,
            }
        )

    parse_stats = {
        "reconstructed_rows": LAST_PARSE_STATS.reconstructed_rows if LAST_PARSE_STATS else 0,
        "parsed_transactions": LAST_PARSE_STATS.output_txns if LAST_PARSE_STATS else 0,
        "failed_rows": LAST_PARSE_STATS.failed_rows if LAST_PARSE_STATS and LAST_PARSE_STATS.failed_rows else [],
        "section_sums": LAST_PARSE_STATS.section_sums if LAST_PARSE_STATS and LAST_PARSE_STATS.section_sums else {},
    }

    return {
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
        pdf_path = DEFAULT_PDF_PATH
    else:
        pdf_path = Path(sys.argv[1]).expanduser()
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}")
        print("Pass a path explicitly: python3 parse_boa_pdf.py <statement.pdf>")
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
