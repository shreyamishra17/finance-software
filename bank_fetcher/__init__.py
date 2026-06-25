from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from bank_fetcher.base_crawler import BankCrawler, FetchResult, SessionExpiredError, TransactionRecord
from bank_fetcher.credential_manager import setup_credentials
from bank_fetcher.pipeline_bridge import PipelineResult, run_pipeline_for_batch

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent

CONFIG_DIR = _MODULE_DIR / "configs"
DEFAULT_RAW_DIR = _PROJECT_ROOT / "my_finance" / "shreya_mishra" / "raw"


def _discover_bank_keys() -> list[str]:
    """Return all bank keys that have a config file in configs/."""
    return sorted(
        p.stem.replace("_fetcher_config", "")
        for p in CONFIG_DIR.glob("*_fetcher_config.json")
    )


def _load_fetcher_config(bank_key: str) -> dict:
    cfg_path = CONFIG_DIR / f"{bank_key}_fetcher_config.json"
    if not cfg_path.exists():
        available = _discover_bank_keys()
        raise FileNotFoundError(
            f"No fetcher config for bank '{bank_key}': {cfg_path}\n"
            f"Available banks: {available}"
        )
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def fetch_statements(
    bank_key: str,
    months: int = 3,
    raw_dir: Path = DEFAULT_RAW_DIR,
    run_pipeline: bool = True,
    username: str = "shreya_mishra",
) -> tuple[FetchResult, list[PipelineResult]]:
    """
    Download statements for bank_key, optionally run the parse pipeline.
    Returns (FetchResult, list[PipelineResult]).
    """
    fetcher_cfg = _load_fetcher_config(bank_key)
    raw_dir.mkdir(parents=True, exist_ok=True)

    crawler = BankCrawler(fetcher_cfg=fetcher_cfg, raw_dir=raw_dir)
    fetch_result = crawler.run(months=months)

    pipeline_results: list[PipelineResult] = []
    if run_pipeline and fetch_result.downloaded:
        pipeline_results = run_pipeline_for_batch(
            fetch_result.downloaded,
            username=username,
        )

    return fetch_result, pipeline_results


def scrape_recent_transactions(
    bank_key: str,
    since_date: datetime | None = None,
    session_save_path: Path | None = None,
) -> tuple[list[dict], str | None, str, str, dict]:
    """
    Login to bank via browser automation and scrape transactions since since_date.
    Returns (transactions, transactions_page_url, ending_balance, starting_balance, extra_values).
    extra_values holds any values captured via "capture" steps (e.g. investment_balance).
    If session_save_path provided, saves session state there for headless reuse.
    """
    fetcher_cfg = _load_fetcher_config(bank_key)
    crawler = BankCrawler(fetcher_cfg=fetcher_cfg, raw_dir=DEFAULT_RAW_DIR)
    records, last_url = crawler.run_scrape(
        since_date=since_date, session_save_path=session_save_path
    )
    txns = [
        {
            "date": r.date,
            "description": r.description,
            "amount": r.amount,
            "type": r.amount_type,
            "balance": r.balance,
        }
        for r in records
    ]
    return txns, last_url, crawler._ending_balance, crawler._starting_balance, crawler._extra_values


def scrape_automated(
    bank_key: str,
    username: str,
    password: str,
    mfa_callback=None,
    since_date: datetime | None = None,
) -> tuple[list[dict], str | None, str, str]:
    """
    Headless automated scrape using provided credentials (Smart Connect).
    Returns (transactions, transactions_page_url, ending_balance, starting_balance).
    """
    fetcher_cfg = _load_fetcher_config(bank_key)
    crawler = BankCrawler(fetcher_cfg=fetcher_cfg, raw_dir=DEFAULT_RAW_DIR)
    records, last_url = crawler.run_scrape_automated(
        username=username,
        password=password,
        mfa_callback=mfa_callback,
        since_date=since_date,
    )
    txns = [
        {
            "date": r.date,
            "description": r.description,
            "amount": r.amount,
            "type": r.amount_type,
            # Must carry balance: it's part of the dedup key in
            # store_transactions(). Dropping it here keyed automated-intake
            # rows with an empty balance, so the same transaction re-synced
            # via the extension (which always sends balance) got a different
            # doc_id and duplicated.
            "balance": r.balance,
        }
        for r in records
    ]
    return txns, last_url, crawler._ending_balance, crawler._starting_balance


def scrape_recent_transactions_headless(
    bank_key: str,
    since_date: datetime | None = None,
    session_path: Path | None = None,
    account_url: str | None = None,
    on_crawler=None,
) -> list[dict]:
    """
    Attempt a silent headless sync using a saved browser session.
    Raises SessionExpiredError if the session is no longer valid.

    on_crawler: optional callable invoked with the freshly-created BankCrawler
    before the scrape starts, so the caller can register it for cooperative
    cancellation (e.g. cancel-on-window-close). It must NOT touch Playwright
    objects from another thread — only call crawler.request_cancel().
    """
    fetcher_cfg = _load_fetcher_config(bank_key)
    crawler = BankCrawler(fetcher_cfg=fetcher_cfg, raw_dir=DEFAULT_RAW_DIR)
    if on_crawler is not None:
        try:
            on_crawler(crawler)
        except Exception:
            pass
    records = crawler.run_scrape_headless(
        since_date=since_date,
        session_path=session_path,
        account_url=account_url,
    )
    return [
        {
            "date": r.date,
            "description": r.description,
            "amount": r.amount,
            "type": r.amount_type,
        }
        for r in records
    ]


__all__ = [
    "fetch_statements",
    "setup_credentials",
    "scrape_recent_transactions",
    "scrape_recent_transactions_headless",
    "SessionExpiredError",
    "_load_fetcher_config",
    "_discover_bank_keys",
    "DEFAULT_RAW_DIR",
    "FetchResult",
    "PipelineResult",
    "TransactionRecord",
]
