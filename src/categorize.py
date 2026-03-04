#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from dataclasses import dataclass
import re
import shutil
from pathlib import Path
from typing import Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "category_rules.json"
DEFAULT_BERT_MODEL_DIR = PROJECT_ROOT / "categorizer" / "models" / "bert_distilbert_50k_full"
CATEGORY_ALIASES: dict[str, str] = {}

# Hardcoded fat-head merchants (layer 1)
FAT_HEAD_MERCHANTS: dict[str, list[str]] = {
    "Amazon": [
        "AMZN",
        "AMZN MKTP",
        "AMAZON.COM",
        "AMZN.COM/BILL",
        "PRIME VDO",
        "AMZN PRIME",
        "AMAZON WEB SERVICES",
        "AWS",
    ],
    "Walmart": ["WM SUPERCENTER", "WAL-MART", "WALMART.COM", "WMT", "WALMART STORE", "WM EZPAY"],
    "Target": ["TARGET T-", "TGT", "TARGET.COM", "TARGET STORE"],
    "Apple": ["APPLE.COM/BILL", "APL* ITUNES", "AAPL", "APPL", "APPLE STORE", "APPLE ONLINE"],
    "Costco": ["COSTCO WHSE", "COSTCO.COM", "COSTCO GAS", "COSTCO AUTO"],
    "Kroger": ["KROGER", "KROGER FUEL", "KROGER #", "PAY AT THE PUMP KROGER"],
    "Whole Foods": ["WHOLEFDS", "WHOLE FOODS", "WFM", "WHOLE FOODS MKT"],
    "Trader Joe's": ["TRADER JOE", "TRADER JOES", "TRADER JOE'S"],
    "Uber": ["UBER *TRIP", "UBER* TRIP", "UBR", "UBER PENDING", "UBER FEE"],
    "Lyft": ["LYFT *RIDE", "LYFT RIDE", "LYFT INC"],
    "Shell": ["SHELL OIL", "SHELL SERVICE", "SHELL PUMP"],
    "Chevron": ["CHEVRON", "CHEVRON/MACONS", "CHEVRON PUMP"],
    "DoorDash": ["DOORDASH", "DD *DOORDASH", "DOORDASH*DASHPASS"],
    "UberEats": ["UBER EATS", "UBER*EATS", "UBEREATS"],
    "Starbucks": ["STARBUCKS", "STARBUCKS STORE", "SBUX", "STARBUCKS APP"],
    "McDonald's": ["MCDONALDS", "MCDONALD'S", "MCD", "MCDONALDS F"],
    "Chick-fil-A": ["CHICK-FIL-A", "CHICKFILA", "CFA"],
    "Netflix": ["NETFLIX.COM", "NETFLIX", "NFLX"],
    "Spotify": ["SPOTIFY USA", "SPOTIFY", "SPTFY"],
    "Hulu": ["HULU", "HULU.COM", "HULU *SUB"],
    "Disney+": ["DISNEY PLUS", "DISNEY+", "DISNEY MOBILE"],
    "CVS": ["CVS/PHARMACY", "CVS PHARMACY", "CVS STORE"],
    "Walgreens": ["WALGREENS", "WALGREENS STORE", "WLGREENS", "WALGRNS"],
}

MERCHANT_CATEGORY: dict[str, str] = {
    "Amazon": "Shopping",
    "Walmart": "Shopping",
    "Target": "Shopping",
    "Apple": "Shopping",
    "Costco": "Groceries",
    "Kroger": "Groceries",
    "Whole Foods": "Groceries",
    "Trader Joe's": "Groceries",
    "Uber": "Transport",
    "Lyft": "Transport",
    "Shell": "Transport",
    "Chevron": "Transport",
    "DoorDash": "Food & Drink",
    "UberEats": "Food & Drink",
    "Starbucks": "Food & Drink",
    "McDonald's": "Food & Drink",
    "Chick-fil-A": "Food & Drink",
    "Netflix": "Entertainment",
    "Spotify": "Entertainment",
    "Hulu": "Entertainment",
    "Disney+": "Entertainment",
    "CVS": "Health",
    "Walgreens": "Health",
}


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


def _normalize_category_name(name: str) -> str:
    return CATEGORY_ALIASES.get(name, name)


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


def _transaction_text(txn: dict) -> str:
    raw = str(txn.get("raw_description") or txn.get("merchant_raw") or "")
    merchant = str(txn.get("merchant_clean") or "")
    return _normalize(" ".join([merchant, raw]))


def _match_fat_head(txn: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    haystack = _transaction_text(txn)
    if not haystack:
        return None, None, None
    for merchant, patterns in FAT_HEAD_MERCHANTS.items():
        for pattern in patterns:
            if _normalize(pattern) in haystack:
                category = _normalize_category_name(MERCHANT_CATEGORY.get(merchant, "Shopping"))
                return category, merchant, pattern
    return None, None, None


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


# Layer 3 fallback: existing keyword rules engine.
def _categorize_by_rules(txn: dict, rules_path: Optional[Path] = None) -> CategoryResult:
    rules = _load_rules(rules_path)

    default_category = _normalize_category_name(str(rules.get("default_category", "Uncategorized")))
    default_confidence = float(rules.get("default_confidence", 0.2))

    raw = str(txn.get("raw_description", ""))
    merchant = str(txn.get("merchant_clean", ""))
    haystack = _normalize(" ".join([merchant, raw]))

    for rule in rules.get("high_confidence", []):
        keywords = rule.get("keywords", [])
        matched = _find_keyword_match(haystack, keywords)
        if matched:
            category = _normalize_category_name(str(rule.get("category", default_category)))
            return CategoryResult(
                category=category,
                confidence=float(rule.get("confidence", 0.9)),
                matched_rule=category,
                matched_keyword=matched,
                layer="rules_high_confidence",
            )

    for rule in rules.get("merchant_rules", []):
        keywords = rule.get("keywords", [])
        matched = _find_keyword_match(_normalize(merchant), keywords) or _find_keyword_match(haystack, keywords)
        if matched:
            category = _normalize_category_name(str(rule.get("category", default_category)))
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

        labels.extend([_normalize_category_name(id2label[int(i)]) for i in idx_np])
        scores.extend([float(x) for x in conf_np])

    return labels, scores


def categorize_transaction(txn: dict, rules_path: Optional[Path] = None) -> CategoryResult:
    # Layer 1: hardcoded fat-head merchants
    category, merchant, pattern = _match_fat_head(txn)
    if category:
        return CategoryResult(
            category=category,
            confidence=1.0,
            matched_rule=merchant,
            matched_keyword=pattern,
            layer="hardcoded",
        )

    # Layer 2: BERT model fallback
    text = str(txn.get("raw_description") or txn.get("merchant_raw") or "")
    if text and _init_bert():
        try:
            labels, scores = _bert_predict_batch([text], batch_size=1)
            return CategoryResult(
                category=labels[0],
                confidence=float(scores[0]),
                matched_rule="bert_model",
                matched_keyword=None,
                layer="bert_model",
            )
        except Exception:
            pass

    # No safety fallback: stay uncategorized if BERT is unavailable.
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
    txns = [dict(txn) for txn in transactions]
    categorized = []

    # Layer 1: hardcoded fat-head merchants.
    bert_idx: list[int] = []
    bert_texts: list[str] = []

    for i, txn in enumerate(txns):
        category, merchant, pattern = _match_fat_head(txn)
        if category:
            txn["category"] = category
            txn["confidence"] = 1.0
            txn["matched_rule"] = merchant
            txn["matched_keyword"] = pattern
            txn["category_layer"] = "hardcoded"
            categorized.append(txn)
            continue

        # Layer 2: keyword rules from category_rules.json.
        rules_result = _categorize_by_rules(txn, rules_path)
        if rules_result.layer != "rules_default":
            txn["category"] = rules_result.category
            txn["confidence"] = rules_result.confidence
            txn["matched_rule"] = rules_result.matched_rule
            txn["matched_keyword"] = rules_result.matched_keyword
            txn["category_layer"] = rules_result.layer
            categorized.append(txn)
            continue

        text = str(txn.get("raw_description") or txn.get("merchant_raw") or "")
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
                categorized[idx]["category"] = label
                categorized[idx]["confidence"] = float(score)
                categorized[idx]["matched_rule"] = "bert_model"
                categorized[idx]["matched_keyword"] = None
                categorized[idx]["category_layer"] = "bert_model"
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


if __name__ == "__main__":
    sample = {
        "raw_description": "MOBILE PURCHASE WHOLEFDS LAJ 100 LA JOLLA CA",
        "merchant_clean": "WHOLEFDS LAJ",
        "amount": "-45.00",
    }
    print(categorize_transaction(sample))
