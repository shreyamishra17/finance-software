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
    "TRANSPORTATION": "Transport",
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
    # Layer 1: keyword rules from category_rules.json
    rules_result = _categorize_by_rules(txn, rules)
    if rules_result.layer != "rules_default":
        return rules_result

    # Layer 2: BERT model fallback
    text = str(txn.get("raw_description") or txn.get("merchant_raw") or "")
    if text and _init_bert():
        try:
            labels, scores = _bert_predict_batch([text], batch_size=1)
            return CategoryResult(
                category=_canonicalize_category_name(labels[0], rules),
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
    rules = _load_rules(rules_path)
    txns = [dict(txn) for txn in transactions]
    categorized = []

    # Layer 1: keyword rules from category_rules.json.
    bert_idx: list[int] = []
    bert_texts: list[str] = []

    for i, txn in enumerate(txns):
        rules_result = _categorize_by_rules(txn, rules)
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

    # Layer 2: BERT for unresolved rows.
    bert_success = False
    if bert_texts and _init_bert():
        try:
            labels, scores = _bert_predict_batch(bert_texts)
            for idx, label, score in zip(bert_idx, labels, scores):
                categorized[idx]["category"] = _canonicalize_category_name(label, rules)
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
