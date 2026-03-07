#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

from rapidfuzz import fuzz, process  # type: ignore


def normalize(text: str) -> str:
    text = str(text).upper().strip()
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def build_reference(companies_csv: Path) -> tuple[list[str], dict[str, list[tuple[str, str]]]]:
    df = pd.read_csv(companies_csv)
    required = {"Company", "Category"}
    if not required.issubset(df.columns):
        raise ValueError(f"{companies_csv} must have columns {required}")

    df = df[["Company", "Category"]].copy()
    df["Company"] = df["Company"].fillna("").astype(str).str.strip()
    df["Category"] = df["Category"].fillna("").astype(str).str.strip()
    df = df[(df["Company"] != "") & (df["Category"] != "")]
    if df.empty:
        raise ValueError("No valid Company/Category rows found.")

    norm_to_original: dict[str, list[tuple[str, str]]] = {}
    for row in df.itertuples(index=False):
        original_company = row.Company
        category = row.Category
        norm = normalize(original_company)
        norm_to_original.setdefault(norm, []).append((original_company, category))

    choices = list(norm_to_original.keys())
    return choices, norm_to_original


def find_top_matches(
    query: str,
    choices: list[str],
    norm_to_original: dict[str, list[tuple[str, str]]],
    top_k: int,
) -> list[dict[str, object]]:
    query_norm = normalize(query)
    if not query_norm:
        return []

    # Step 0: direct substring match in merchant text (e.g., "STARBUCKS STORE" -> "STARBUCKS").
    substring_hits: list[str] = []
    for candidate in choices:
        compact_len = len(candidate.replace(" ", ""))
        if compact_len < 4:
            continue
        pattern = r"\b" + re.escape(candidate).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, query_norm):
            substring_hits.append(candidate)

    if substring_hits:
        substring_hits.sort(key=lambda s: len(s.replace(" ", "")), reverse=True)
        results: list[dict[str, object]] = []
        for matched_norm in substring_hits[:top_k]:
            original_company, category = norm_to_original[matched_norm][0]
            results.append(
                {
                    "input_merchant": query,
                    "matched_company": original_company,
                    "category": category,
                    "score": 100.0,
                }
            )
        return results

    results: list[dict[str, object]] = []
    matches = process.extract(
        query_norm,
        choices,
        scorer=fuzz.WRatio,
        limit=top_k,
    )
    for matched_norm, score, _ in matches:
        original_company, category = norm_to_original[matched_norm][0]
        results.append(
            {
                "input_merchant": query,
                "matched_company": original_company,
                "category": category,
                "score": round(float(score), 2),
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fast fuzzy comparison of a merchant string against the hardcoded companies CSV."
    )
    parser.add_argument(
        "--companies-csv",
        type=Path,
        default=Path("categorizer/datasets/hardcoded_companies_for_classification.csv"),
        help="Reference CSV with Company and Category columns.",
    )
    parser.add_argument(
        "--merchant",
        type=str,
        help="Single merchant string to compare.",
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        help="Optional input CSV for batch mode.",
    )
    parser.add_argument(
        "--text-col",
        default="merchant_clean",
        help="Text column in --input-csv.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of top fuzzy matches to return per input.",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("categorizer/datasets/merchant_fuzzy_matches.csv"),
        help="Output CSV for batch mode.",
    )
    args = parser.parse_args()

    if not args.companies_csv.exists():
        raise FileNotFoundError(f"Reference file not found: {args.companies_csv}")

    if not args.merchant and not args.input_csv:
        raise ValueError("Provide either --merchant or --input-csv.")

    choices, norm_to_original = build_reference(args.companies_csv)

    if args.merchant:
        top = find_top_matches(args.merchant, choices, norm_to_original, args.top_k)
        if not top:
            print("No match (empty merchant).")
            return
        for i, row in enumerate(top, start=1):
            print(
                f"{i}. match={row['matched_company']} | category={row['category']} | score={row['score']}"
            )
        return

    if not args.input_csv or not args.input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {args.input_csv}")

    df = pd.read_csv(args.input_csv)
    if args.text_col not in df.columns:
        raise ValueError(f"Column '{args.text_col}' not found in {args.input_csv}")

    output_rows: list[dict[str, object]] = []
    for merchant in df[args.text_col].fillna("").astype(str):
        top = find_top_matches(merchant, choices, norm_to_original, args.top_k)
        if not top:
            output_rows.append(
                {
                    "input_merchant": merchant,
                    "rank": 1,
                    "matched_company": "",
                    "category": "",
                    "score": 0.0,
                }
            )
            continue
        for rank, row in enumerate(top, start=1):
            output_rows.append(
                {
                    "input_merchant": row["input_merchant"],
                    "rank": rank,
                    "matched_company": row["matched_company"],
                    "category": row["category"],
                    "score": row["score"],
                }
            )

    out = pd.DataFrame(output_rows)
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.output_csv, index=False)
    print(f"Saved fuzzy match results to: {args.output_csv}")
    print(f"Rows: {len(out)}")


if __name__ == "__main__":
    main()
