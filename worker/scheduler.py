"""
worker/scheduler.py — APScheduler setup for background account refresh.

The scheduler runs inside the FastAPI process (no separate worker needed for MVP).
Jobs run every REFRESH_INTERVAL_HOURS hours (default 8).

Environment variables:
  REFRESH_INTERVAL_HOURS  — how often to sync all accounts (default: 8)
  DISABLE_SCHEDULER       — set to "1" to skip starting the scheduler
"""
from __future__ import annotations

import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

_scheduler: BackgroundScheduler | None = None

# How often to CHECK which accounts are due (not how often to sync each account)
# Per-account sync frequency is controlled by sync_interval_hours in the DB
_CHECK_INTERVAL_MINUTES = int(os.environ.get("REFRESH_CHECK_MINUTES", "30"))


def _run_refresh() -> None:
    """Wrapper so scheduler exceptions don't kill the process."""
    try:
        from worker.refresh import refresh_all
        refresh_all()
    except Exception as exc:
        print(f"[scheduler] refresh_all() raised: {exc}")


def start_scheduler() -> None:
    """Start the background scheduler. Called once at app startup."""
    global _scheduler

    if os.environ.get("DISABLE_SCHEDULER", "").strip() in ("1", "true", "yes"):
        print("[scheduler] Disabled via DISABLE_SCHEDULER env var.")
        return

    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(
        job_defaults={"misfire_grace_time": 60 * 60},  # 1-hour grace
        timezone="UTC",
    )
    _scheduler.add_job(
        _run_refresh,
        trigger=IntervalTrigger(minutes=_CHECK_INTERVAL_MINUTES),
        id="refresh_all",
        name="Check and refresh due accounts",
        replace_existing=True,
    )
    _scheduler.start()
    print(f"[scheduler] Started — checking due accounts every {_CHECK_INTERVAL_MINUTES}min.")


def stop_scheduler() -> None:
    """Gracefully stop the scheduler. Called at app shutdown."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        print("[scheduler] Stopped.")


def trigger_now() -> None:
    """Trigger an immediate refresh run (useful for testing)."""
    _run_refresh()
