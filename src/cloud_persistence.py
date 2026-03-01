#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import os
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional

import firebase_admin
from firebase_admin import credentials, storage

from parse_boa_pdf import CSV_COLUMNS, _build_csv_rows, _mask_account_number, _statement_range_slug


def _sanitize_slug(value: str, fallback: str = "unknown") -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in value.strip()).strip("_").lower()
    return cleaned or fallback


def _first_last_slug(*candidates: Optional[str]) -> str:
    for value in candidates:
        if not value:
            continue
        tokens = [t for t in "".join(ch if ch.isalnum() else " " for ch in str(value)).split() if t]
        if len(tokens) >= 2:
            return _sanitize_slug(f"{tokens[0]}_{tokens[1]}", fallback="unknown_user")
    for value in candidates:
        if not value:
            continue
        cleaned = _sanitize_slug(str(value), fallback="")
        if cleaned:
            return cleaned
    return "unknown_user"


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    buff = io.StringIO()
    writer = csv.DictWriter(buff, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: (v if str(v).strip() else "NA") for k, v in row.items()})
    return buff.getvalue().encode("utf-8")


def _csv_row_key(row: dict[str, str], account_scope: str = "") -> str:
    merchant_raw = str(row.get("merchant_raw", "NA") or "NA")
    merchant_norm = "".join(ch.lower() if ch.isalnum() else " " for ch in merchant_raw)
    merchant_norm = " ".join(merchant_norm.split())
    date_norm = str(row.get("date", "NA") or "NA").strip()
    ref_norm = str(row.get("reference_id", "NA") or "NA").strip().upper()
    amount_raw = str(row.get("amount", "NA") or "NA").strip()
    try:
        from parse_boa_pdf import _parse_amount  # lazy import to avoid cycle at module import time
    except ModuleNotFoundError:
        from src.parse_boa_pdf import _parse_amount
    amount_parsed = _parse_amount(amount_raw)
    amount_norm = f"{amount_parsed:.2f}" if amount_parsed is not None else amount_raw
    key = "|".join([account_scope, date_norm, merchant_norm, amount_norm, ref_norm])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _read_csv_rows(csv_data: bytes) -> list[dict[str, str]]:
    text = csv_data.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict[str, str]] = []
    for row in reader:
        out.append({k: (row.get(k, "NA") or "NA") for k in CSV_COLUMNS})
    return out


def _merge_rows(
    existing: list[dict[str, str]],
    incoming: list[dict[str, str]],
    account_scope: str,
) -> list[dict[str, str]]:
    merged_map: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for row in existing:
        key = _csv_row_key(row, account_scope=account_scope)
        if key not in merged_map:
            order.append(key)
        merged_map[key] = row
    for row in incoming:
        key = _csv_row_key(row, account_scope=account_scope)
        if key not in merged_map:
            order.append(key)
        merged_map[key] = row
    return [merged_map[k] for k in order]


def _init_bucket() -> storage.Bucket:
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.ApplicationDefault())
    bucket_name = os.environ.get("FIREBASE_STORAGE_BUCKET", "").strip()
    if not bucket_name:
        raise RuntimeError("FIREBASE_STORAGE_BUCKET is required for firebase persistence mode")
    return storage.bucket(bucket_name)


def persist_payload_firebase_storage(
    payload: dict,
    username: Optional[str],
    source_file: str,
    pdf_bytes: bytes,
) -> dict[str, str]:
    bucket = _init_bucket()
    info = payload.get("statement_info", {})
    user_slug = _first_last_slug(username, info.get("owner_name"), info.get("account_name"))
    account_id = _mask_account_number(str(info.get("account_number") or ""))
    rows = _build_csv_rows(payload, source_file)

    # raw/
    period = info.get("statement_period")
    period_slug = _statement_range_slug(period, Path(source_file))
    source_slug = _sanitize_slug(Path(source_file).stem, "statement")
    raw_blob_path = f"{user_slug}/raw/{period_slug}_{account_id}_{source_slug}.pdf"
    raw_blob = bucket.blob(raw_blob_path)
    if raw_blob.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        raw_blob_path = f"{user_slug}/raw/{period_slug}_{account_id}_{source_slug}_{stamp}.pdf"
        raw_blob = bucket.blob(raw_blob_path)
    raw_blob.upload_from_string(pdf_bytes, content_type="application/pdf")

    # processed/
    csv_blob_path = f"{user_slug}/processed/acc_{account_id}.csv"
    csv_blob = bucket.blob(csv_blob_path)
    if csv_blob.exists():
        existing = _read_csv_rows(csv_blob.download_as_bytes())
        rows = _merge_rows(existing, rows, account_scope=account_id)
    csv_blob.upload_from_string(_csv_bytes(rows), content_type="text/csv; charset=utf-8")

    return {
        "mode": "firebase_storage",
        "raw_blob": raw_blob_path,
        "csv_blob": csv_blob_path,
    }
