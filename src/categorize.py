#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, date
import re
import shutil
from pathlib import Path
from typing import Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "category_rules.json"
DEFAULT_BERT_MODEL_DIR = PROJECT_ROOT / "categorizer" / "models" / "distilbert_companies"
DEFAULT_RAPIDFUZZ_CSV = PROJECT_ROOT / "categorizer" / "datasets" / "hardcoded_companies_for_classification.csv"
RAPIDFUZZ_THRESHOLD = 90  # 0–100 scale; requires >=0.9 confidence
BERT_MIN_CONFIDENCE = 0.7  # Only accept BERT if score is strictly greater than this.

# Make categorizer/src importable for step_1
_CATEGORIZER_SRC = str(PROJECT_ROOT / "categorizer" / "src")
if _CATEGORIZER_SRC not in sys.path:
    sys.path.insert(0, _CATEGORIZER_SRC)

try:
    from categorize_step_1 import categorize_step_1 as _categorize_step_1  # type: ignore
    _STEP1_AVAILABLE = True
except ImportError:
    _STEP1_AVAILABLE = False

DEFAULT_CATEGORY_ALIASES: dict[str, str] = {
    "CASH & ATM": "Finance",
    "ENTERTAINMENT & RECREATION": "Entertainment",
    "FINANCIAL SERVICES": "Finance",
    "FOOD & DINING": "Food & Drink",
    "FOOD & DRINK/GROCERIES": "Groceries",
    "HEALTH/SHOPPING": "Health",
    "HEALTHCARE & MEDICAL": "Health",
    "OTHER/MISC": "Miscellaneous",
    "SHOPPING & RETAIL": "Shopping",
    "TRANSPORTATION": "Auto & Transport",
    "TRANSFER": "Finance",
    "UTILITIES & SERVICES": "Utilities & Bills",
}

# Cache for loaded rules — avoids re-reading the file for every transaction in a batch
_RULES_DATA: Optional[dict] = None
_RULES_PATH_LOADED: Optional[Path] = None


@dataclass
class CategoryResult:
    category: str
    confidence: float
    matched_rule: Optional[str] = None
    matched_keyword: Optional[str] = None
    layer: str = "default"


_BERT_CACHE: dict[str, object] = {
    "ready": False,
    "failed": False,
    "model": None,
    "tokenizer": None,
    "device": None,
    "model_dir": None,
}


def _load_rules(path: Optional[Path]) -> dict:
    global _RULES_DATA, _RULES_PATH_LOADED
    rules_path = path or DEFAULT_RULES_PATH
    if _RULES_DATA is not None and _RULES_PATH_LOADED == rules_path:
        return _RULES_DATA
    try:
        with rules_path.open("r", encoding="utf-8") as fh:
            _RULES_DATA = json.load(fh)
            _RULES_PATH_LOADED = rules_path
    except (OSError, json.JSONDecodeError):
        return {
            "default_category": "Uncategorized",
            "default_confidence": 0.2,
            "high_confidence": [],
            "merchant_rules": [],
        }
    return _RULES_DATA


def _normalize(text: str) -> str:
    return " ".join(text.upper().split())


def _canonical_categories(rules: dict) -> dict[str, str]:
    categories: dict[str, str] = {}
    default_category = str(rules.get("default_category", "Uncategorized")).strip()
    if default_category:
        categories[_normalize(default_category)] = default_category
    for rule in rules.get("high_confidence", []):
        cat = str(rule.get("category", "")).strip()
        if cat:
            categories[_normalize(cat)] = cat
    for rule in rules.get("merchant_rules", []):
        cat = str(rule.get("category", "")).strip()
        if cat:
            categories[_normalize(cat)] = cat
    return categories


def _canonicalize_category_name(name: str, rules: dict) -> str:
    raw = str(name or "").strip()
    if not raw:
        return str(rules.get("default_category", "Uncategorized"))

    canon = _canonical_categories(rules)
    raw_norm = _normalize(raw)
    if raw_norm in canon:
        return canon[raw_norm]

    aliases: dict[str, str] = {}
    aliases.update({k.upper(): v for k, v in DEFAULT_CATEGORY_ALIASES.items()})
    cfg_aliases = rules.get("category_aliases", {})
    if isinstance(cfg_aliases, dict):
        aliases.update(
            {
                _normalize(str(k)): str(v).strip()
                for k, v in cfg_aliases.items()
                if str(k).strip() and str(v).strip()
            }
        )

    # Some model labels include extra metadata: "Transfer (ID : Chewy)"
    base = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip()
    base_norm = _normalize(base)

    candidate = aliases.get(raw_norm) or aliases.get(base_norm)
    if not candidate and base_norm.startswith("TRANSFER"):
        candidate = "Finance"

    if candidate:
        cand_norm = _normalize(candidate)
        if cand_norm in canon:
            return canon[cand_norm]
        return candidate

    return raw


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


def _statement_category_markers(rules: dict) -> list[str]:
    markers = rules.get("statement_category_markers", [])
    if not isinstance(markers, list):
        return []
    normalized: list[str] = []
    for marker in markers:
        text = str(marker).strip()
        if text:
            normalized.append(_normalize(text))
    return sorted(set(normalized), key=len, reverse=True)


def _categorize_from_statement_tail(txn: dict, rules: dict) -> Optional[CategoryResult]:
    raw = str(txn.get("raw_description") or txn.get("merchant_raw") or "").strip()
    if not raw:
        return None
    raw_norm = _normalize(raw)
    merchant_norm = _normalize(str(txn.get("merchant_clean") or ""))

    for marker in _statement_category_markers(rules):
        if not re.search(rf"(?:^|[\s:,\-/]){re.escape(marker)}$", raw_norm):
            continue
        if merchant_norm.endswith(marker):
            continue
        category = _canonicalize_category_name(marker, rules)
        return CategoryResult(
            category=category,
            confidence=0.99,
            matched_rule="statement_category_tail",
            matched_keyword=marker,
            layer="statement_category",
        )
    return None


def _select_device(torch_module):
    if hasattr(torch_module.backends, "mps") and torch_module.backends.mps.is_available():
        return torch_module.device("mps")
    if torch_module.cuda.is_available():
        return torch_module.device("cuda")
    return torch_module.device("cpu")


def _parse_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI: {uri}")
    no_scheme = uri[5:]
    bucket, _, prefix = no_scheme.partition("/")
    if not bucket or not prefix:
        raise ValueError(f"GCS URI must include bucket and object prefix: {uri}")
    return bucket, prefix.rstrip("/") + "/"


def _download_model_from_gcs(gcs_uri: str) -> Path:
    from google.cloud import storage  # type: ignore

    bucket_name, prefix = _parse_gs_uri(gcs_uri)
    local_root = Path(os.environ.get("CATEGORIZER_MODEL_CACHE_DIR", "/tmp/categorizer_model_cache"))
    local_dir = local_root / prefix.strip("/").split("/")[-1]
    config_path = local_dir / "config.json"
    if config_path.exists():
        return local_dir

    local_dir.mkdir(parents=True, exist_ok=True)
    client = storage.Client()
    blobs = list(client.list_blobs(bucket_name, prefix=prefix))
    if not blobs:
        raise RuntimeError(f"No model files found at {gcs_uri}")

    for blob in blobs:
        name = blob.name
        if name.endswith("/"):
            continue
        rel = name[len(prefix) :] if name.startswith(prefix) else Path(name).name
        target = local_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(target))

    if not config_path.exists():
        shutil.rmtree(local_dir, ignore_errors=True)
        raise RuntimeError(f"Downloaded files from {gcs_uri} but config.json not found")
    return local_dir


def _resolve_model_dir() -> Optional[Path]:
    explicit_dir = Path(os.environ.get("CATEGORIZER_MODEL_DIR", str(DEFAULT_BERT_MODEL_DIR)))
    if (explicit_dir / "config.json").exists():
        return explicit_dir

    gcs_uri = os.environ.get("CATEGORIZER_MODEL_GCS_URI", "").strip()
    if gcs_uri:
        return _download_model_from_gcs(gcs_uri)
    return None


def _init_bert() -> bool:
    if _BERT_CACHE["ready"]:
        return True
    if _BERT_CACHE["failed"]:
        return False
    if os.environ.get("CATEGORIZER_DISABLE_BERT", "").strip().lower() in {"1", "true", "yes"}:
        _BERT_CACHE["failed"] = True
        return False

    model_dir = _resolve_model_dir()
    if model_dir is None:
        _BERT_CACHE["failed"] = True
        return False

    try:
        import torch  # type: ignore
        from transformers import AutoModelForSequenceClassification, AutoTokenizer  # type: ignore

        tokenizer = AutoTokenizer.from_pretrained(model_dir)
        model = AutoModelForSequenceClassification.from_pretrained(model_dir)
        device = _select_device(torch)
        model.to(device)
        model.eval()

        _BERT_CACHE["tokenizer"] = tokenizer
        _BERT_CACHE["model"] = model
        _BERT_CACHE["device"] = device
        _BERT_CACHE["model_dir"] = str(model_dir)
        _BERT_CACHE["ready"] = True
        return True
    except Exception:
        _BERT_CACHE["failed"] = True
        return False


_RAPIDFUZZ_REFERENCE: Optional[list[tuple[str, str]]] = None
_RAPIDFUZZ_REFERENCE_LOADED: bool = False


def _load_rapidfuzz_reference() -> list[tuple[str, str]]:
    global _RAPIDFUZZ_REFERENCE, _RAPIDFUZZ_REFERENCE_LOADED
    if _RAPIDFUZZ_REFERENCE_LOADED:
        return _RAPIDFUZZ_REFERENCE or []
    _RAPIDFUZZ_REFERENCE_LOADED = True
    csv_path = Path(os.environ.get("RAPIDFUZZ_REFERENCE_CSV", str(DEFAULT_RAPIDFUZZ_CSV)))
    if not csv_path.exists():
        return []
    try:
        rows: list[tuple[str, str]] = []
        with csv_path.open("r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                company = str(row.get("Company", "")).strip()
                category = str(row.get("Category", "")).strip()
                if company and category:
                    rows.append((company, category))
        _RAPIDFUZZ_REFERENCE = rows
    except Exception:
        _RAPIDFUZZ_REFERENCE = []
    return _RAPIDFUZZ_REFERENCE or []


def _categorize_by_rapidfuzz(txn: dict, rules: dict) -> Optional[CategoryResult]:
    merchant = str(txn.get("merchant_clean") or "").strip()
    if len(merchant) < 3:
        return None
    reference = _load_rapidfuzz_reference()
    if not reference:
        return None
    try:
        from rapidfuzz import process, fuzz, utils  # type: ignore
        companies = [c for c, _ in reference]
        result = process.extractOne(
            merchant, companies, scorer=fuzz.WRatio, processor=utils.default_process
        )
        if result is None or result[1] < RAPIDFUZZ_THRESHOLD:
            return None
        matched_company = result[0]
        category_raw = next((cat for comp, cat in reference if comp == matched_company), None)
        if not category_raw:
            return None
        return CategoryResult(
            category=_canonicalize_category_name(category_raw, rules),
            confidence=round(result[1] / 100.0, 4),
            matched_rule="rapidfuzz",
            matched_keyword=matched_company,
            layer="rapidfuzz",
        )
    except Exception:
        return None


# Layer 1: keyword rules engine.
def _categorize_by_rules(txn: dict, rules: dict) -> CategoryResult:
    default_category = _canonicalize_category_name(str(rules.get("default_category", "Uncategorized")), rules)
    default_confidence = float(rules.get("default_confidence", 0.2))

    raw = str(txn.get("raw_description") or txn.get("merchant_raw") or "")
    merchant = str(txn.get("merchant_clean") or "")
    haystack = _normalize(" ".join([merchant, raw]))

    for rule in rules.get("high_confidence", []):
        keywords = rule.get("keywords", [])
        matched = _find_keyword_match(haystack, keywords)
        if matched:
            category = _canonicalize_category_name(str(rule.get("category", default_category)), rules)
            return CategoryResult(
                category=category,
                confidence=float(rule.get("confidence", 0.9)),
                matched_rule=category,
                matched_keyword=matched,
                layer="rules_high_confidence",
            )

    # Amount-based rules: override category when charge is below a threshold (e.g. small Amazon = subscription)
    amount_val = _parse_amount_value(txn.get("amount"))
    if amount_val is not None:
        abs_amount = abs(amount_val)
        for rule in rules.get("amount_rules", []):
            max_amt = rule.get("max_amount_abs")
            if max_amt is not None and abs_amount < float(max_amt):
                matched = _find_keyword_match(haystack, rule.get("keywords", []))
                if matched:
                    category = _canonicalize_category_name(str(rule.get("category", default_category)), rules)
                    return CategoryResult(
                        category=category,
                        confidence=float(rule.get("confidence", 0.75)),
                        matched_rule=category,
                        matched_keyword=matched,
                        layer="rules_amount",
                    )

    for rule in rules.get("merchant_rules", []):
        keywords = rule.get("keywords", [])
        matched = _find_keyword_match(_normalize(merchant), keywords) or _find_keyword_match(haystack, keywords)
        if matched:
            category = _canonicalize_category_name(str(rule.get("category", default_category)), rules)
            return CategoryResult(
                category=category,
                confidence=float(rule.get("confidence", 0.6)),
                matched_rule=category,
                matched_keyword=matched,
                layer="rules_merchant",
            )

    return CategoryResult(
        category=default_category,
        confidence=default_confidence,
        matched_rule=default_category,
        matched_keyword=None,
        layer="rules_default",
    )


def _as_numpy(array_like):
    try:
        return array_like.cpu().numpy()
    except Exception:
        return array_like


def _parse_amount_value(raw_amount: object) -> Optional[float]:
    text = str(raw_amount or "").strip()
    if not text or text.upper() == "UNPARSED":
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if text.startswith("-"):
        negative = True
        text = text[1:]
    if text.startswith("+"):
        text = text[1:]
    text = text.replace("$", "").replace(",", "")
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def _income_allowed(txn: dict) -> bool:
    txn_type = str(txn.get("txn_type") or "").lower()
    if txn_type == "deposits":
        return True
    if txn_type == "withdrawals":
        return False
    amount_val = _parse_amount_value(txn.get("amount"))
    return amount_val is not None and amount_val > 0


def _post_validate_category(txn: dict, result: CategoryResult, rules: dict) -> CategoryResult:
    if result.category == "Income" and not _income_allowed(txn):
        return CategoryResult(
            category=_canonicalize_category_name(str(rules.get("default_category", "Uncategorized")), rules),
            confidence=0.0,
            matched_rule="income_sign_guard",
            matched_keyword=result.matched_keyword,
            layer="post_validation",
        )
    return result


def _bert_predict_batch(texts: list[str], batch_size: int = 64, max_length: int = 64) -> tuple[list[str], list[float]]:
    import torch  # type: ignore

    if not _init_bert():
        raise RuntimeError("BERT model unavailable")

    tokenizer = _BERT_CACHE["tokenizer"]
    model = _BERT_CACHE["model"]
    device = _BERT_CACHE["device"]
    id2label = model.config.id2label

    labels: list[str] = []
    scores: list[float] = []

    for start in range(0, len(texts), batch_size):
        chunk = texts[start : start + batch_size]
        enc = tokenizer(
            chunk,
            truncation=True,
            padding=True,
            max_length=max_length,
            return_tensors="pt",
        )
        inputs = {k: v.to(device) for k, v in enc.items()}
        with torch.no_grad():
            logits = model(**inputs).logits
            probs = torch.softmax(logits, dim=1)
            conf, idx = probs.max(dim=1)

        idx_np = _as_numpy(idx)
        conf_np = _as_numpy(conf)

        labels.extend([id2label[int(i)] for i in idx_np])
        scores.extend([float(x) for x in conf_np])

    return labels, scores


def categorize_transaction(txn: dict, rules_path: Optional[Path] = None) -> CategoryResult:
    rules = _load_rules(rules_path)

    # Layer 1: step_1 high-confidence pattern rules (Zelle, Direct Dep, TST*)
    if _STEP1_AVAILABLE:
        step1_text = str(txn.get("raw_description") or txn.get("merchant_raw") or txn.get("merchant_clean") or "")
        step1 = _categorize_step_1(step1_text)
        if step1 is not None:
            return _post_validate_category(txn, CategoryResult(
                category=_canonicalize_category_name(step1.category, rules),
                confidence=step1.confidence,
                matched_rule="step_1",
                matched_keyword=step1.predicted_by,
                layer="step_1",
            ), rules)

    # Layer 2: rapidfuzz fuzzy merchant matching
    fuzz_result = _categorize_by_rapidfuzz(txn, rules)
    if fuzz_result is not None:
        return _post_validate_category(txn, fuzz_result, rules)

    # Layer 3: BERT model fallback (use clean merchant string)
    text = str(txn.get("merchant_clean") or "")
    if text and _init_bert():
        try:
            labels, scores = _bert_predict_batch([text], batch_size=1)
            bert_score = float(scores[0])
            if bert_score <= BERT_MIN_CONFIDENCE:
                return CategoryResult(
                    category="Uncategorized",
                    confidence=bert_score,
                    matched_rule="bert_low_confidence",
                    matched_keyword=None,
                    layer="bert_low_confidence",
                )
            return _post_validate_category(
                txn,
                CategoryResult(
                    category=_canonicalize_category_name(labels[0], rules),
                    confidence=bert_score,
                    matched_rule="bert_model",
                    matched_keyword=None,
                    layer="bert_model",
                ),
                rules,
            )
        except Exception:
            pass

    return CategoryResult(
        category="Uncategorized",
        confidence=0.0,
        matched_rule="bert_unavailable",
        matched_keyword=None,
        layer="bert_unavailable",
    )


def categorize_transactions(
    transactions: Iterable[dict],
    rules_path: Optional[Path] = None,
) -> list[dict]:
    rules = _load_rules(rules_path)
    txns = [dict(txn) for txn in transactions]
    categorized = []

    bert_idx: list[int] = []
    bert_texts: list[str] = []

    for i, txn in enumerate(txns):
        # Layer 1: step_1 high-confidence pattern rules
        if _STEP1_AVAILABLE:
            step1_text = str(txn.get("raw_description") or txn.get("merchant_raw") or txn.get("merchant_clean") or "")
            step1 = _categorize_step_1(step1_text)
            if step1 is not None:
                result = _post_validate_category(txn, CategoryResult(
                    category=_canonicalize_category_name(step1.category, rules),
                    confidence=step1.confidence,
                    matched_rule="step_1",
                    matched_keyword=step1.predicted_by,
                    layer="step_1",
                ), rules)
                txn["category"] = result.category
                txn["confidence"] = result.confidence
                txn["matched_rule"] = result.matched_rule
                txn["matched_keyword"] = result.matched_keyword
                txn["category_layer"] = result.layer
                categorized.append(txn)
                continue

        # Layer 2: rapidfuzz fuzzy merchant matching
        fuzz_result = _categorize_by_rapidfuzz(txn, rules)
        if fuzz_result is not None:
            result = _post_validate_category(txn, fuzz_result, rules)
            txn["category"] = result.category
            txn["confidence"] = result.confidence
            txn["matched_rule"] = result.matched_rule
            txn["matched_keyword"] = result.matched_keyword
            txn["category_layer"] = result.layer
            categorized.append(txn)
            continue

        # Layer 3: BERT — collect for batch inference (use clean merchant string)
        text = str(txn.get("merchant_clean") or "")
        if text:
            bert_idx.append(i)
            bert_texts.append(text)
        categorized.append(txn)

    # Layer 3: BERT for unresolved rows.
    bert_success = False
    if bert_texts and _init_bert():
        try:
            labels, scores = _bert_predict_batch(bert_texts)
            for idx, label, score in zip(bert_idx, labels, scores):
                score_val = float(score)
                if score_val <= BERT_MIN_CONFIDENCE:
                    categorized[idx]["category"] = "Uncategorized"
                    categorized[idx]["confidence"] = score_val
                    categorized[idx]["matched_rule"] = "bert_low_confidence"
                    categorized[idx]["matched_keyword"] = None
                    categorized[idx]["category_layer"] = "bert_low_confidence"
                    continue
                candidate = CategoryResult(
                    category=_canonicalize_category_name(label, rules),
                    confidence=score_val,
                    matched_rule="bert_model",
                    matched_keyword=None,
                    layer="bert_model",
                )
                final_result = _post_validate_category(categorized[idx], candidate, rules)
                categorized[idx]["category"] = final_result.category
                categorized[idx]["confidence"] = final_result.confidence
                categorized[idx]["matched_rule"] = final_result.matched_rule
                categorized[idx]["matched_keyword"] = final_result.matched_keyword
                categorized[idx]["category_layer"] = final_result.layer
            bert_success = True
        except Exception:
            bert_success = False

    # No safety fallback: unresolved rows are explicitly uncategorized when BERT is unavailable.
    if not bert_success:
        for idx in bert_idx:
            categorized[idx]["category"] = "Uncategorized"
            categorized[idx]["confidence"] = 0.0
            categorized[idx]["matched_rule"] = "bert_unavailable"
            categorized[idx]["matched_keyword"] = None
            categorized[idx]["category_layer"] = "bert_unavailable"

    return categorized


# ── Recurrent-income detection ────────────────────────────────────────────
# Some employers/payers (e.g. "QUALCOMM TECHNOL") aren't in the keyword rules
# and won't match PAYROLL/DIRECT DEP, so their deposits land as Uncategorized.
# This post-pass groups credits by merchant_clean and promotes any group with
# a regular cadence + consistent amount to "Income". Skipped for credit-card
# accounts because there a "credit" means a refund/payment, not income.

def _parse_txn_date(raw) -> Optional[date]:
    if not raw:
        return None
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d %b %Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%m-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except (ValueError, TypeError):
        return None


def detect_recurrent_income(
    transactions: list[dict],
    account_type: Optional[str] = None,
    min_occurrences: int = 3,
    expected_periods_days: tuple = (7, 14, 15, 30, 31),
    period_tolerance_pct: float = 0.25,
    amount_tolerance_pct: float = 0.30,
) -> list[dict]:
    """Promote regular merchant-level credits to category "Income".

    A merchant's credits are promoted when:
      - there are at least `min_occurrences` of them
      - the median day-gap between consecutive dates matches one of
        `expected_periods_days` within `period_tolerance_pct`
      - the per-occurrence amounts are within `amount_tolerance_pct` of their mean
    Already-categorized Income/Transfer/Refund rows are left alone. Credit-card
    accounts are skipped entirely (every "credit" there is a payment, not income).
    """
    if (account_type or "").lower() == "credit_card":
        return transactions
    if not transactions:
        return transactions

    groups: dict[str, list[dict]] = {}
    for txn in transactions:
        existing = str(txn.get("category") or "").lower()
        if existing in {"income", "transfer", "refund"}:
            continue
        amount = _parse_amount_value(txn.get("amount"))
        # Direction. `amount_type` is the authoritative field the scrapers set
        # ("credit"/"debit"); `txn_type`/`type` are categorizer-input aliases.
        # CRITICAL: scraped amounts are stored as ABSOLUTE values, so `amount > 0`
        # is NOT a direction signal — using it promoted every recurring DEBIT
        # (e.g. ICICI SIP investments "ACH/FIN NJ INDIA INVEST", mutual-fund
        # mandates) to "Income". Trust the explicit direction; only fall back to
        # the amount sign when none is present, and only a real negative = debit.
        amount_type = str(txn.get("amount_type") or "").lower()
        txn_type = str(txn.get("txn_type") or txn.get("type") or "").lower()
        if amount_type.startswith(("debit", "withdrawal")) or txn_type in {"debit", "withdrawal", "withdrawals"}:
            is_credit = False
        elif amount_type == "credit" or txn_type in {"credit", "deposits", "deposit"}:
            is_credit = True
        else:
            is_credit = amount is not None and amount > 0
        if not is_credit:
            continue
        merchant = (txn.get("merchant_clean") or txn.get("merchant_raw") or "").strip().upper()
        if not merchant:
            continue
        groups.setdefault(merchant, []).append(txn)

    for merchant, txns in groups.items():
        if len(txns) < min_occurrences:
            continue
        dated = [(t, _parse_txn_date(t.get("date"))) for t in txns]
        dated = [(t, d) for t, d in dated if d is not None]
        if len(dated) < min_occurrences:
            continue
        dated.sort(key=lambda td: td[1])
        gaps = [(dated[i + 1][1] - dated[i][1]).days for i in range(len(dated) - 1)]
        gaps = [g for g in gaps if g > 0]
        if not gaps:
            continue
        median_gap = sorted(gaps)[len(gaps) // 2]
        if not any(abs(median_gap - p) <= p * period_tolerance_pct for p in expected_periods_days):
            continue
        amounts = [abs(_parse_amount_value(t.get("amount")) or 0.0) for t, _ in dated]
        amounts = [a for a in amounts if a > 0]
        if len(amounts) < min_occurrences:
            continue
        mean_amt = sum(amounts) / len(amounts)
        if mean_amt == 0:
            continue
        if max(abs(a - mean_amt) / mean_amt for a in amounts) > amount_tolerance_pct:
            continue

        for t, _ in dated:
            t["category"] = "Income"
            t["matched_rule"] = "recurrent_income_heuristic"
            t["matched_keyword"] = merchant
            t["category_layer"] = "recurrence_post_pass"
            t["confidence"] = 0.85

    return transactions


if __name__ == "__main__":
    sample = {
        "raw_description": "MOBILE PURCHASE WHOLEFDS LAJ 100 LA JOLLA CA",
        "merchant_clean": "WHOLEFDS LAJ",
        "amount": "-45.00",
    }
    print(categorize_transaction(sample))
