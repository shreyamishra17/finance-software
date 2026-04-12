"""
core/state_machine.py — Connection status state machine for linked accounts.

States
------
pending          Account row created, first sync not yet attempted.
connected        Last sync succeeded; data is fresh.
refreshing       Background sync in progress.
stale            Sync succeeded but data is >24 h old.
otp_required     Bank demanded OTP during automated sync (needs user action).
reauth_required  Session expired or full re-login is required.
broken           Repeated failures; manual intervention needed.

Transitions
-----------
Only allowed moves are defined in _TRANSITIONS. Use transition() to update
account status safely — it will raise ValueError for invalid moves.
"""
from __future__ import annotations

from enum import Enum


class ConnStatus(str, Enum):
    PENDING         = "pending"
    CONNECTED       = "connected"
    REFRESHING      = "refreshing"
    STALE           = "stale"
    OTP_REQUIRED    = "otp_required"
    REAUTH_REQUIRED = "reauth_required"
    BROKEN          = "broken"


_TRANSITIONS: dict[ConnStatus, set[ConnStatus]] = {
    ConnStatus.PENDING: {
        ConnStatus.CONNECTED,
        ConnStatus.REAUTH_REQUIRED,
        ConnStatus.BROKEN,
    },
    ConnStatus.CONNECTED: {
        ConnStatus.REFRESHING,
        ConnStatus.REAUTH_REQUIRED,
        ConnStatus.STALE,
    },
    ConnStatus.REFRESHING: {
        ConnStatus.CONNECTED,
        ConnStatus.STALE,
        ConnStatus.OTP_REQUIRED,
        ConnStatus.REAUTH_REQUIRED,
        ConnStatus.BROKEN,
    },
    ConnStatus.STALE: {
        ConnStatus.REFRESHING,
        ConnStatus.REAUTH_REQUIRED,
        ConnStatus.BROKEN,
    },
    ConnStatus.OTP_REQUIRED: {
        ConnStatus.CONNECTED,
        ConnStatus.REAUTH_REQUIRED,
        ConnStatus.BROKEN,
    },
    ConnStatus.REAUTH_REQUIRED: {
        ConnStatus.PENDING,
        ConnStatus.CONNECTED,
        ConnStatus.BROKEN,
    },
    ConnStatus.BROKEN: {
        ConnStatus.PENDING,
    },
}


def can_transition(from_status: str, to_status: str) -> bool:
    try:
        f = ConnStatus(from_status)
        t = ConnStatus(to_status)
        return t in _TRANSITIONS.get(f, set())
    except ValueError:
        return False


def transition(from_status: str, to_status: str) -> str:
    """Return to_status if valid, else raise ValueError."""
    if not can_transition(from_status, to_status):
        raise ValueError(
            f"Invalid transition: {from_status!r} → {to_status!r}"
        )
    return to_status


def needs_user_action(status: str) -> bool:
    """True when the status requires the user to manually reconnect."""
    return status in {
        ConnStatus.OTP_REQUIRED,
        ConnStatus.REAUTH_REQUIRED,
        ConnStatus.BROKEN,
    }


def is_healthy(status: str) -> bool:
    return status in {ConnStatus.CONNECTED, ConnStatus.REFRESHING}


def status_label(status: str) -> str:
    """Human-readable label for display in the UI."""
    return {
        "pending":         "Pending",
        "connected":       "Connected",
        "refreshing":      "Syncing…",
        "stale":           "Stale",
        "otp_required":    "OTP Required",
        "reauth_required": "Reconnect Required",
        "broken":          "Broken",
    }.get(status, status.replace("_", " ").title())
