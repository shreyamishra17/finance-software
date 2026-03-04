#!/usr/bin/env python3
from __future__ import annotations

import argparse
import random
import re
import string
from pathlib import Path

import pandas as pd
try:
    from faker import Faker  # type: ignore
except Exception:  # pragma: no cover
    Faker = None  # type: ignore


class _FallbackFaker:
    """Fallback generator used when faker is unavailable in the runtime."""

    _cities = [
        "SAN DIEGO",
        "LOS ANGELES",
        "NEW YORK",
        "SEATTLE",
        "DALLAS",
        "AUSTIN",
        "CHICAGO",
        "PHOENIX",
    ]
    _states = ["CA", "NY", "TX", "WA", "IL", "AZ", "FL", "CO"]

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def city(self) -> str:
        return self._rng.choice(self._cities)

    def state_abbr(self) -> str:
        return self._rng.choice(self._states)


def apply_payment_prefix(text: str, category: str, rng: random.Random) -> str:
    """Prepend a gateway prefix, with category-aware consistency."""
    category_upper = category.upper()

    # Keep gateway style consistent with category type while ensuring
    # SQ* appears broadly across SMB-heavy categories (not just food).
    if "FOOD" in category_upper or "DINING" in category_upper or "BEVERAGE" in category_upper:
        prefixes = ["TST* ", "SQ * "]
        weights = [0.85, 0.15]
    elif "SHOPPING" in category_upper or "RETAIL" in category_upper:
        prefixes = ["SQ * ", "AMZ* ", "SP * ", ""]
        weights = [0.10, 0.55, 0.05, 0.30]
    elif "SUBSCRIPTION" in category_upper or "ONLINE" in category_upper:
        prefixes = ["SQ * ", "SP * ", "AMZ* ", ""]
        weights = [0.10, 0.05, 0.55, 0.30]
    elif any(
        tag in category_upper
        for tag in [
            "HEALTH",
            "MEDICAL",
            "FITNESS",
            "WELLNESS",
            "AUTO",
            "HOME",
            "SERVICES",
            "PERSONAL",
            "EDUCATION",
            "PET",
            "TRAVEL",
            "ENTERTAINMENT",
            "RECREATION",
        ]
    ):
        prefixes = ["SQ * ", "SP * ", ""]
        weights = [0.10, 0.05, 0.85]
    elif any(tag in category_upper for tag in ["INCOME", "GOVERNMENT", "TAX", "LEGAL", "BANK", "FINANCIAL"]):
        prefixes = ["SP * ", "SQ * ", ""]
        weights = [0.08, 0.04, 0.88]
    else:
        prefixes = ["SQ * ", "SP * ", "AMZ* ", ""]
        weights = [0.10, 0.05, 0.55, 0.30]

    prefix = rng.choices(prefixes, weights=weights, k=1)[0]
    return f"{prefix}{text}" if prefix else text


def apply_geo_or_date_stamp(text: str, fake, rng: random.Random) -> str:
    """Either prepend MM/DD or append CITY ST."""
    if rng.random() < 0.5:
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        return f"{month:02d}/{day:02d} {text}"

    city = fake.city().upper()
    try:
        state = fake.state_abbr().upper()
    except Exception:
        state = rng.choice(["CA", "TX", "NY", "FL", "WA"])
    return f"{text} {city} {state}"


def apply_reference_padding(text: str, rng: random.Random) -> str:
    """Append REF/TXN/# and uppercase alphanumeric token."""
    token_len = rng.randint(6, 10)
    token = "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(token_len))
    marker = rng.choice(["REF", "TXN", "#", ""])
    return f"{text} {marker}{token}".strip()


def apply_vowel_drop(text: str, rng: random.Random) -> str:
    """Remove vowels from words longer than 5 characters with low probability."""
    vowels = set("AEIOUaeiou")
    transformed_words = []
    for word in text.split():
        if len(word) > 5 and rng.random() < 0.3:
            transformed_words.append("".join(ch for ch in word if ch not in vowels))
        else:
            transformed_words.append(word)
    cleaned = " ".join(transformed_words)
    return re.sub(r"\s+", " ", cleaned).strip()


def apply_mainframe_truncation(text: str, rng: random.Random) -> str:
    """Truncate to 30 characters to emulate less severe legacy cuts."""
    max_len = 30
    return text[:max_len].rstrip()


def apply_mobile_purchase_tag(text: str, rng: random.Random) -> str:
    """Inject 'MOBILE PURCHASE' at start, near end, or at end."""
    tag = "MOBILE PURCHASE"
    mode = rng.choice(["start", "before_city", "end"])

    if mode == "start":
        return f"{tag} {text}"

    tokens = text.split()
    if mode == "before_city" and len(tokens) >= 4:
        # If final token looks like state abbreviation, insert before last 2 tokens.
        if len(tokens[-1]) == 2 and tokens[-1].isalpha():
            insert_at = max(1, len(tokens) - 2)
            tokens.insert(insert_at, tag)
            return " ".join(tokens)

    return f"{text} {tag}"


def inject_noise(base_text: str, category: str, fake, rng: random.Random) -> str:
    """
    Apply at least 3 noise injections to a base merchant description.
    Truncation, when selected, is always applied last.
    """
    operations = ["prefix", "geo_date", "ref_pad", "vowel_drop", "mobile_purchase", "truncate"]
    chosen = rng.sample(operations, k=rng.randint(3, 6))

    text = base_text
    truncate_selected = "truncate" in chosen

    for op in chosen:
        if op == "truncate":
            continue
        if op == "prefix":
            text = apply_payment_prefix(text, category, rng)
        elif op == "geo_date":
            text = apply_geo_or_date_stamp(text, fake, rng)
        elif op == "ref_pad":
            text = apply_reference_padding(text, rng)
        elif op == "vowel_drop":
            text = apply_vowel_drop(text, rng)
        elif op == "mobile_purchase":
            text = apply_mobile_purchase_tag(text, rng)

    if truncate_selected:
        text = apply_mainframe_truncation(text, rng)

    return text


def build_augmented_dataset(
    input_csv: Path, output_csv: Path, n_rows: int, seed: int, ordered: bool
) -> None:
    rng = random.Random(seed)
    if Faker is not None:
        Faker.seed(seed)
        fake = Faker("en_US")
    else:
        fake = _FallbackFaker(rng)

    df = pd.read_csv(input_csv, usecols=["transaction_description", "category"])
    df = df.dropna(subset=["transaction_description", "category"]).copy()
    df["transaction_description"] = df["transaction_description"].astype(str).str.strip()
    df["category"] = df["category"].astype(str).str.strip()
    df = df[(df["transaction_description"] != "") & (df["category"] != "")]

    if df.empty:
        raise ValueError("Input dataset has no usable rows after cleaning.")

    if ordered:
        sampled = df.head(n_rows).copy()
    else:
        sampled = df.sample(n=n_rows, replace=True, random_state=seed)

    rows = []
    for _, row in sampled.iterrows():
        base_text = row["transaction_description"]
        label = row["category"]
        noisy = inject_noise(base_text, label, fake, rng)
        rows.append({"raw_string": noisy, "category": label})

    out_df = pd.DataFrame(rows, columns=["raw_string", "category"])
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(output_csv, index=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate noisy transaction strings for classification training."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("categorizer/datasets/dataset_large.csv"),
        help="Input CSV containing transaction_description and category columns.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("categorizer/simulated_data_large.csv"),
        help="Output CSV path.",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=10000,
        help="Number of augmented rows to generate.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility.",
    )
    parser.add_argument(
        "--ordered",
        action="store_true",
        help="Take rows in source order (first N rows) instead of random sampling.",
    )
    args = parser.parse_args()

    build_augmented_dataset(args.input, args.output, args.rows, args.seed, args.ordered)
    print(f"Generated {args.rows} rows at {args.output}")


if __name__ == "__main__":
    main()
