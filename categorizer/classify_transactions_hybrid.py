#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

import pandas as pd
import torch
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModelForSequenceClassification, AutoTokenizer


FAT_HEAD_MERCHANTS: dict[str, list[str]] = {
    # --- E-COMMERCE & RETAIL ---
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
    "Walmart": [
        "WM SUPERCENTER",
        "WAL-MART",
        "WALMART.COM",
        "WMT",
        "WALMART STORE",
        "WM EZPAY",
    ],
    "Target": ["TARGET T-", "TGT", "TARGET.COM", "TARGET STORE"],
    "Apple": ["APPLE.COM/BILL", "APL* ITUNES", "AAPL", "APPL", "APPLE STORE", "APPLE ONLINE"],
    # --- GROCERIES & WHOLESALE ---
    "Costco": ["COSTCO WHSE", "COSTCO.COM", "COSTCO GAS", "COSTCO AUTO"],
    "Kroger": ["KROGER", "KROGER FUEL", "KROGER #", "PAY AT THE PUMP KROGER"],
    "Whole Foods": ["WHOLEFDS", "WHOLE FOODS", "WFM", "WHOLE FOODS MKT"],
    "Trader Joe's": ["TRADER JOE", "TRADER JOES", "TRADER JOE'S"],
    # --- TRANSPORTATION & RIDESHARE ---
    "Uber": ["UBER *TRIP", "UBER* TRIP", "UBR", "UBER PENDING", "UBER FEE"],
    "Lyft": ["LYFT *RIDE", "LYFT RIDE", "LYFT INC"],
    "Shell": ["SHELL OIL", "SHELL SERVICE", "SHELL PUMP"],
    "Chevron": ["CHEVRON", "CHEVRON/MACONS", "CHEVRON PUMP"],
    # --- FOOD DELIVERY & DINING ---
    "DoorDash": ["DOORDASH", "DD *DOORDASH", "DOORDASH*DASHPASS"],
    "UberEats": ["UBER EATS", "UBER*EATS", "UBEREATS"],
    "Starbucks": ["STARBUCKS", "STARBUCKS STORE", "SBUX", "STARBUCKS APP"],
    "McDonald's": ["MCDONALDS", "MCDONALD'S", "MCD", "MCDONALDS F"],
    "Chick-fil-A": ["CHICK-FIL-A", "CHICKFILA", "CFA"],
    # --- SUBSCRIPTIONS & ENTERTAINMENT ---
    "Netflix": ["NETFLIX.COM", "NETFLIX", "NFLX"],
    "Spotify": ["SPOTIFY USA", "SPOTIFY", "SPTFY"],
    "Hulu": ["HULU", "HULU.COM", "HULU *SUB"],
    "Disney+": ["DISNEY PLUS", "DISNEY+", "DISNEY MOBILE"],
    # --- PHARMACIES & CONVENIENCE ---
    "CVS": ["CVS/PHARMACY", "CVS PHARMACY", "CVS STORE"],
    "Walgreens": ["WALGREENS", "WALGREENS STORE", "WLGREENS", "WALGRNS"],
}


MERCHANT_CATEGORY: dict[str, str] = {
    "Amazon": "Shopping",
    "Walmart": "Shopping",
    "Target": "Shopping",
    "Apple": "Shopping",
    "Costco": "Food & Drink",
    "Kroger": "Food & Drink",
    "Whole Foods": "Food & Drink",
    "Trader Joe's": "Food & Drink",
    "Uber": "Transportation",
    "Lyft": "Transportation",
    "Shell": "Transportation",
    "Chevron": "Transportation",
    "DoorDash": "Food & Dining",
    "UberEats": "Food & Dining",
    "Starbucks": "Food & Dining",
    "McDonald's": "Food & Dining",
    "Chick-fil-A": "Food & Dining",
    "Netflix": "Entertainment",
    "Spotify": "Entertainment",
    "Hulu": "Entertainment",
    "Disney+": "Entertainment",
    "CVS": "Health",
    "Walgreens": "Health",
}


class InferenceDataset(Dataset):
    def __init__(self, input_ids: torch.Tensor, attention_mask: torch.Tensor):
        self.input_ids = input_ids
        self.attention_mask = attention_mask

    def __len__(self) -> int:
        return len(self.input_ids)

    def __getitem__(self, idx: int) -> dict[str, torch.Tensor]:
        return {
            "input_ids": self.input_ids[idx],
            "attention_mask": self.attention_mask[idx],
        }


def select_device() -> torch.device:
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def find_text_col(df: pd.DataFrame, preferred: Optional[str]) -> str:
    if preferred and preferred in df.columns:
        return preferred
    for col in ["raw_string", "merchant_raw", "string", "transaction_description"]:
        if col in df.columns:
            return col
    raise ValueError("No text column found. Provide --text-col explicitly.")


def hardcoded_match(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    txt = text.upper()
    for merchant, patterns in FAT_HEAD_MERCHANTS.items():
        for pat in patterns:
            if pat.upper() in txt:
                return merchant, pat, MERCHANT_CATEGORY[merchant]
    return None, None, None


@torch.no_grad()
def bert_predict(
    texts: list[str],
    model_dir: Path,
    batch_size: int,
    max_length: int,
) -> tuple[list[str], list[float]]:
    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModelForSequenceClassification.from_pretrained(model_dir)
    device = select_device()
    print(f"Running BERT inference on: {device}")
    model.to(device)
    model.eval()

    enc = tokenizer(
        texts,
        truncation=True,
        padding=True,
        max_length=max_length,
        return_tensors="pt",
    )
    ds = InferenceDataset(enc["input_ids"], enc["attention_mask"])
    dl = DataLoader(ds, batch_size=batch_size, shuffle=False)

    labels: list[str] = []
    scores: list[float] = []
    id2label = model.config.id2label

    for batch in dl:
        inputs = {k: v.to(device) for k, v in batch.items()}
        logits = model(**inputs).logits
        probs = torch.softmax(logits, dim=1)
        conf, idx = probs.max(dim=1)
        labels.extend([id2label[int(i)] for i in idx.cpu().numpy()])
        scores.extend([float(x) for x in conf.cpu().numpy()])

    return labels, scores


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hybrid transaction classifier: hardcoded fat-head + BERT fallback."
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        default=Path("my_finance/processed/2025-03-12_to_2025-04-10_transactions.csv"),
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("categorizer/hybrid_predictions.csv"),
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path("categorizer/models/bert_distilbert_50k_full"),
    )
    parser.add_argument("--text-col", default=None)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-length", type=int, default=64)
    args = parser.parse_args()

    df = pd.read_csv(args.input_csv)
    text_col = find_text_col(df, args.text_col)
    texts = df[text_col].fillna("").astype(str).tolist()

    rows: list[dict] = []
    fallback_texts: list[str] = []
    fallback_idx: list[int] = []

    for i, text in enumerate(texts):
        merchant, pattern, category = hardcoded_match(text)
        if category is not None:
            rows.append(
                {
                    "raw_string": text,
                    "category": category,
                    "score": 1.0,
                    "layer": "hardcoded",
                    "matched_merchant": merchant,
                    "matched_pattern": pattern,
                }
            )
        else:
            rows.append(
                {
                    "raw_string": text,
                    "category": None,
                    "score": None,
                    "layer": "bert",
                    "matched_merchant": None,
                    "matched_pattern": None,
                }
            )
            fallback_texts.append(text)
            fallback_idx.append(i)

    if fallback_texts:
        pred_labels, pred_scores = bert_predict(
            texts=fallback_texts,
            model_dir=args.model_dir,
            batch_size=args.batch_size,
            max_length=args.max_length,
        )
        for ridx, label, score in zip(fallback_idx, pred_labels, pred_scores):
            rows[ridx]["category"] = label
            rows[ridx]["score"] = round(score, 4)

    out_df = pd.DataFrame(rows)
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(args.output_csv, index=False)
    print(f"Saved: {args.output_csv}")
    print(f"Rows: {len(out_df)}")
    print(f"Hardcoded matches: {sum(out_df['layer'] == 'hardcoded')}")
    print(f"BERT fallbacks: {sum(out_df['layer'] == 'bert')}")


if __name__ == "__main__":
    main()
