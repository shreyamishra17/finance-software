#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from categorize_step_1 import categorize_step_1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Combine RapidFuzz and DistilBERT predictions with threshold-based routing."
    )
    parser.add_argument(
        "--rapidfuzz-csv",
        type=Path,
        default=Path("categorizer/datasets/merchant_clean_rapidfuzz_categories.csv"),
    )
    parser.add_argument(
        "--distilbert-csv",
        type=Path,
        default=Path("categorizer/datasets/merchant_clean_distilbert_categories.csv"),
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("categorizer/datasets/merchant_clean_hybrid_rapidfuzz_distilbert.csv"),
    )
    parser.add_argument(
        "--raw-input-csv",
        type=Path,
        default=Path("my_finance/processed/2025-03-12_to_2025-04-10_transactions.csv"),
        help="CSV containing raw transaction strings for step_1 rules.",
    )
    parser.add_argument(
        "--raw-col",
        default="merchant_raw",
        help="Raw-string column name in --raw-input-csv used by step_1.",
    )
    parser.add_argument(
        "--rapidfuzz-threshold",
        type=float,
        default=0.95,
        help="Use RapidFuzz category when normalized score > threshold, else try DistilBERT.",
    )
    parser.add_argument(
        "--distilbert-threshold",
        type=float,
        default=0.7,
        help="Use DistilBERT category only when score > threshold; otherwise Uncategorized.",
    )
    args = parser.parse_args()

    if not args.rapidfuzz_csv.exists():
        raise FileNotFoundError(f"RapidFuzz CSV not found: {args.rapidfuzz_csv}")
    if not args.distilbert_csv.exists():
        raise FileNotFoundError(f"DistilBERT CSV not found: {args.distilbert_csv}")
    if not args.raw_input_csv.exists():
        raise FileNotFoundError(f"Raw input CSV not found: {args.raw_input_csv}")

    rf = pd.read_csv(args.rapidfuzz_csv)
    db = pd.read_csv(args.distilbert_csv)
    raw_df = pd.read_csv(args.raw_input_csv)

    required_rf = {"input_merchant", "category", "score"}
    required_db = {"merchant_clean", "distilbert_category", "distilbert_score"}
    if not required_rf.issubset(rf.columns):
        raise ValueError(f"RapidFuzz CSV must contain {required_rf}.")
    if not required_db.issubset(db.columns):
        raise ValueError(f"DistilBERT CSV must contain {required_db}.")
    if len(rf) != len(db):
        raise ValueError(f"Row count mismatch: rapidfuzz={len(rf)} distilbert={len(db)}")
    if args.raw_col not in raw_df.columns:
        raise ValueError(f"Column '{args.raw_col}' not found in {args.raw_input_csv}")
    if len(raw_df) != len(rf):
        raise ValueError(f"Row count mismatch: raw_input={len(raw_df)} predictions={len(rf)}")

    rf_merchant = rf["input_merchant"].fillna("").astype(str).reset_index(drop=True)
    db_merchant = db["merchant_clean"].fillna("").astype(str).reset_index(drop=True)
    raw_text = raw_df[args.raw_col].fillna("").astype(str).reset_index(drop=True)
    if not rf_merchant.equals(db_merchant):
        mismatch_count = int((rf_merchant != db_merchant).sum())
        raise ValueError(f"Merchant row-order mismatch between files ({mismatch_count} mismatches).")

    # rapidfuzz script outputs percentages (0-100); normalize to 0-1.
    rf_score = rf["score"].astype(float) / 100.0
    use_rf = rf_score > args.rapidfuzz_threshold

    final_category = []
    final_confidence = []
    predicted_by = []
    for i in range(len(rf)):
        step1 = categorize_step_1(str(raw_text.iloc[i]))
        if step1 is not None:
            final_category.append(step1.category)
            final_confidence.append(step1.confidence)
            predicted_by.append(step1.predicted_by)
        elif bool(use_rf.iloc[i]):
            final_category.append(str(rf.iloc[i]["category"]))
            final_confidence.append(float(rf_score.iloc[i]))
            predicted_by.append("rapidfuzz")
        else:
            db_score = float(db.iloc[i]["distilbert_score"])
            if db_score > args.distilbert_threshold:
                final_category.append(str(db.iloc[i]["distilbert_category"]))
                final_confidence.append(db_score)
                predicted_by.append("distilbert")
            else:
                final_category.append("Uncategorized")
                final_confidence.append(db_score)
                predicted_by.append("none")

    out = pd.DataFrame(
        {
            "merchant_clean": db_merchant,
            "final_category": final_category,
            "confidence": [round(v, 6) for v in final_confidence],
            "predicted_by": predicted_by,
            "distilbert_category": db["distilbert_category"].astype(str),
        }
    )

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.output_csv, index=False)

    print(f"Saved: {args.output_csv}")
    print(f"Rows: {len(out)}")
    print(f"Used step_1: {int((out['predicted_by'] == 'step_1').sum())}")
    print(f"Used rapidfuzz: {int((out['predicted_by'] == 'rapidfuzz').sum())}")
    print(f"Used distilbert: {int((out['predicted_by'] == 'distilbert').sum())}")
    print(f"Uncategorized: {int((out['predicted_by'] == 'none').sum())}")


if __name__ == "__main__":
    main()
