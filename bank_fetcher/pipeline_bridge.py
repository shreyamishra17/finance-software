from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Make src/ importable when running bank_fetcher as a standalone module
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = str(_PROJECT_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from parse_bank import parse_pdf_to_payload, persist_uploaded_statement  # noqa: E402
from categorize import categorize_transactions  # noqa: E402
from new_bank_setup import bootstrap_config_if_unknown  # noqa: E402


@dataclass
class PipelineResult:
    pdf_path: Path
    saved_pdf_path: Optional[str] = None
    account_csv_path: Optional[str] = None
    transaction_count: int = 0
    error: Optional[str] = None
    success: bool = False


def run_pipeline_for_pdf(
    pdf_path: Path,
    username: str = "shreya_mishra",
    auto_bootstrap: bool = True,
) -> PipelineResult:
    """
    Feed one downloaded PDF through the full parse → categorize → persist pipeline.
    Mirrors the logic in serve_demo.py's DemoHandler.do_POST() without the HTTP layer.
    """
    result = PipelineResult(pdf_path=pdf_path)
    try:
        if auto_bootstrap:
            bootstrap_config_if_unknown(pdf_path)

        payload = parse_pdf_to_payload(pdf_path)
        payload["transactions"] = categorize_transactions(payload.get("transactions", []))

        storage = persist_uploaded_statement(
            payload=payload,
            original_filename=pdf_path.name,
            pdf_bytes=pdf_path.read_bytes(),
            username=username,
        )

        result.saved_pdf_path = storage.get("saved_pdf_path") or storage.get("raw_path")
        result.account_csv_path = storage.get("account_csv_path") or storage.get("csv_path")
        result.transaction_count = len(payload.get("transactions", []))
        result.success = True
    except Exception as exc:
        result.error = str(exc)
    return result


def run_pipeline_for_batch(
    pdf_paths: list[Path],
    username: str = "shreya_mishra",
    auto_bootstrap: bool = True,
) -> list[PipelineResult]:
    """Process a list of downloaded PDFs sequentially."""
    return [
        run_pipeline_for_pdf(p, username=username, auto_bootstrap=auto_bootstrap)
        for p in pdf_paths
    ]
