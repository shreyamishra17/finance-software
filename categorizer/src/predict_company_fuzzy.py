#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd


def normalize(text: str) -> str:
    text = text.upper().strip()
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def score_pair(query_raw: str, candidate_raw: str) -> float:
    q = normalize(query_raw)
    c = normalize(candidate_raw)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0

    # Full-string similarity.
    ratio_full = SequenceMatcher(None, q, c).ratio()

    # Space-insensitive similarity for POS-like compact strings.
    q_ns = q.replace(" ", "")
    c_ns = c.replace(" ", "")
    ratio_nospace = SequenceMatcher(None, q_ns, c_ns).ratio()

    # Reward substring-style near matches (e.g., "WHOLEFDS" vs "WHOLE FOODS").
    contain = 0.0
    if q_ns in c_ns or c_ns in q_ns:
        contain = min(len(q_ns), len(c_ns)) / max(len(q_ns), len(c_ns))

    # Token overlap helps multi-word merchant strings.
    q_tokens = set(q.split())
    c_tokens = set(c.split())
    if q_tokens and c_tokens:
        jaccard = len(q_tokens & c_tokens) / len(q_tokens | c_tokens)
    else:
        jaccard = 0.0

    return max(ratio_full, ratio_nospace, contain, jaccard)


def build_reference(df_ref: pd.DataFrame) -> list[tuple[str, str]]:
    rows = df_ref[["Company", "Category"]].copy()
    rows["Company"] = rows["Company"].fillna("").astype(str).str.strip()
    rows["Category"] = rows["Category"].fillna("").astype(str).str.strip()
    rows = rows[(rows["Company"] != "") & (rows["Category"] != "")]
    return list(rows.itertuples(index=False, name=None))


def best_match(query: str, reference: list[tuple[str, str]]) -> tuple[str, str, float]:
    best_company = ""
    best_category = ""
    best_score = -1.0

    for company, category in reference:
        s = score_pair(query, company)
        if s > best_score:
            best_company = company
            best_category = category
            best_score = s

    if best_score < 0:
        return "", "", 0.0
    return best_company, best_category, float(best_score)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fuzzy match merchant strings to known companies and output category + similarity score."
    )
    parser.add_argument(
        "--reference-csv",
        type=Path,
        default=Path("categorizer/datasets/hardcoded_companies_for_classification.csv"),
        help="CSV containing known merchants with columns Company and Category.",
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        required=True,
        help="CSV containing merchant strings to classify.",
    )
    parser.add_argument(
        "--text-col",
        default="merchant_clean",
        help="Column in input CSV containing merchant text.",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("categorizer/datasets/merchant_clean_fuzzy_categories.csv"),
        help="Output CSV path.",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.0,
        help="If best similarity is below this threshold, predicted_category becomes Uncategorized.",
    )
    args = parser.parse_args()

    if not args.reference_csv.exists():
        raise FileNotFoundError(f"Reference CSV not found: {args.reference_csv}")
    if not args.input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {args.input_csv}")

    df_ref = pd.read_csv(args.reference_csv)
    needed = {"Company", "Category"}
    if not needed.issubset(df_ref.columns):
        raise ValueError(f"Reference CSV must contain columns {needed}, got {set(df_ref.columns)}")

    df_in = pd.read_csv(args.input_csv)
    if args.text_col not in df_in.columns:
        raise ValueError(f"Column '{args.text_col}' not found in {args.input_csv}")

    reference = build_reference(df_ref)
    if not reference:
        raise ValueError("No valid rows in reference CSV after cleaning.")

    merchants = df_in[args.text_col].fillna("").astype(str)

    matched_company = []
    predicted_category = []
    similarity_score = []

    for m in merchants:
        company, category, score = best_match(m, reference)
        matched_company.append(company)
        if score >= args.min_score:
            predicted_category.append(category)
        else:
            predicted_category.append("Uncategorized")
        similarity_score.append(round(score, 6))

    out = pd.DataFrame(
        {
            args.text_col: merchants,
            "matched_company": matched_company,
            "predicted_category": predicted_category,
            "similarity_score": similarity_score,
        }
    )

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.output_csv, index=False)

    print(f"Saved fuzzy predictions to: {args.output_csv}")
    print(f"Rows: {len(out)}")


if __name__ == "__main__":
    main()
