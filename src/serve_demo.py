#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import queue as _queue
import re
import tempfile
import threading
import time
import uuid as _uuid
import webbrowser
from datetime import datetime, timedelta
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv

from auth import create_user, authenticate_user, issue_token, verify_token, delete_user, update_password, update_profile
from categorize import categorize_transactions, detect_recurrent_income
from parse_bank import parse_pdf_to_payload, persist_uploaded_statement, merchant_clean as _merchant_clean_fn
from new_bank_setup import bootstrap_config_if_unknown
from db import (
    init_db, add_account, get_accounts, get_account,
    delete_account, store_transactions, get_transactions, update_last_synced,
    set_auto_sync, set_needs_login, set_last_url, set_account_balance,
    set_has_credentials,
    set_account_status, get_latest_transaction_date, set_investment_balance,
    set_current_uid, reset_current_uid, get_current_uid, list_user_ids,
)


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
WEB_ROOT = ROOT / "website"

# ── Active-scrape registry (for cancel-on-window-close) ───────────────────────
# Maps uid → set of live BankCrawler objects currently running a headless
# scrape for that user. When the user closes the FIFO window, the browser sends
# a /api/sync/cancel beacon; we look up the uid's crawlers and call
# request_cancel() on each (a thread-safe bool flip — never touches Playwright
# from this thread). The scrape thread notices at its next checkpoint and
# unwinds cleanly, closing the headless browser in its own thread.
_ACTIVE_SCRAPES: dict[str, set] = {}
_ACTIVE_SCRAPES_LOCK = threading.Lock()


def _register_scrape(uid: str, crawler) -> None:
    with _ACTIVE_SCRAPES_LOCK:
        _ACTIVE_SCRAPES.setdefault(uid or "anon", set()).add(crawler)


def _unregister_scrape(uid: str, crawler) -> None:
    with _ACTIVE_SCRAPES_LOCK:
        bucket = _ACTIVE_SCRAPES.get(uid or "anon")
        if bucket:
            bucket.discard(crawler)
            if not bucket:
                _ACTIVE_SCRAPES.pop(uid or "anon", None)


def _cancel_scrapes(uid: str) -> int:
    """Request cancellation of every active scrape for this uid. Returns the
    number signalled. Safe to call from any thread — only flips a bool."""
    with _ACTIVE_SCRAPES_LOCK:
        bucket = list(_ACTIVE_SCRAPES.get(uid or "anon", set()))
    for crawler in bucket:
        try:
            crawler.request_cancel()
        except Exception:
            pass
    return len(bucket)

# ── ACH / BofA metadata stripping ────────────────────────────────────────────
_ACH_DES_RE   = re.compile(r'\s+DES\s*:.*$', re.IGNORECASE)
_ACH_ID_RE    = re.compile(r'\s+ID\s*:\s*\S+.*$', re.IGNORECASE)
_ACH_INDN_RE  = re.compile(r'\s+INDN\s*:.*$', re.IGNORECASE)
_ACH_COID_RE  = re.compile(r'\s+CO\s+ID\s*:.*$', re.IGNORECASE)
_ACH_HOLD_RE  = re.compile(r'^ACH\s+HOLD\s+', re.IGNORECASE)
_ACH_PMTS_RE  = re.compile(r'\s+(WEB\s+)?PMTS\s+ON\s+\d{1,2}/\d{1,2}\s*$', re.IGNORECASE)
_ACH_WEB_RE   = re.compile(r'\s+WEB\s*$', re.IGNORECASE)
_VIEW_EDIT_RE = re.compile(r'\s*View/Edit\s*$', re.IGNORECASE)


def _parse_amount(raw) -> float | None:
    """Parse a transaction amount string from any bank into a float.

    Handles all currency symbols ($, ₹, €, £, ¥), Indian lakh-format commas,
    leading +/- signs, and plain decimals.  Returns None if unparseable so
    callers can skip the row cleanly — never silently returns 0.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    # Strip all known currency symbols and whitespace
    s = s.replace("$", "").replace("₹", "").replace("€", "").replace("£", "").replace("¥", "")
    # Remove thousands/lakh separators (commas)
    s = s.replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _normalize_merchant_name(raw: str) -> str:
    """Strip BofA/ACH metadata so all payment variants of the same merchant group together.

    Examples:
      "GARDENHOMESMANAG DES:WEB PMTS ID:CS06RL INDN:... WEB View/Edit" → "Gardenhomesmanag"
      "ACH HOLD GARDENHOMESMANAG WEB PMTS ON 04/29"                    → "Gardenhomesmanag"
      "DISCOVER DES:E-PAYMENT ID:3384 INDN:MISHRA SHREYA CO ID:..."    → "Discover"
    """
    s = (raw or "").strip()
    s = _VIEW_EDIT_RE.sub("", s).strip()
    des = _ACH_DES_RE.search(s)
    if des:
        s = s[:des.start()].strip()
    else:
        s = _ACH_ID_RE.sub("", s).strip()
        s = _ACH_INDN_RE.sub("", s).strip()
        s = _ACH_COID_RE.sub("", s).strip()
    s = _ACH_HOLD_RE.sub("", s).strip()
    s = _ACH_PMTS_RE.sub("", s).strip()
    s = _ACH_WEB_RE.sub("", s).strip()
    if s and s == s.upper():
        s = s.title()
    return s or raw


# Description patterns that strongly indicate money coming IN. We err on the
# side of caution — only flip a row to "credit" when one of these clearly
# matches, otherwise leave it as "debit". Better to under-count income than to
# misclassify a spend as income.
_CREDIT_DESCRIPTION_PATTERNS = (
    "DIRECT DEP", "DIRECT DEPOSIT", "DES:DIRECT DEP",
    "PAYROLL", "SALARY", "WAGES", "BIWEEKLY",
    "ACH CREDIT", "CREDIT FROM",
    "INTEREST PAID", "INTEREST EARNED", "INTEREST INCOME", "DIVIDEND",
    "REFUND", "REIMBURSEMENT", "REBATE", "CASH BACK", "CASHBACK",
    "MOBILE DEPOSIT", "CHECK DEPOSIT", "ATM DEPOSIT",
    "ZELLE FROM", "ZELLE PAYMENT FROM",
    "TRANSFER FROM", "FROM CHK", "FROM SAV", "ONLINE BANKING TRANSFER FROM",
    "VENMO CASHOUT", "PAYPAL TRANSFER",
    "TAX REFUND", "IRS TREAS", "STATE TAX",
    "QUALCOMM",  # known payer for this user
    "BONUS", "COMMISSION", "STIPEND", "FREELANCE",
)


def _infer_amount_type(
    description: str,
    merchant_clean: str,
    category: str,
    account_type: str,
) -> str:
    """Best-effort inference of credit vs debit when the stored row doesn't
    carry a signed amount. Used by the recategorize endpoint to repair
    incorrectly-tagged historical data without re-scraping the bank."""
    haystack = f"{(description or '').upper()} {(merchant_clean or '').upper()}"
    cat = (category or "").strip().lower()
    if cat in {"income", "refund"}:
        return "credit"
    if any(p in haystack for p in _CREDIT_DESCRIPTION_PATTERNS):
        return "credit"
    return "charge" if account_type == "credit_card" else "debit"


def _account_has_credentials(bank_key: Optional[str]) -> bool:
    """True if the user has saved a username/password for this bank, in either
    the OS keychain (local dev) or Firestore (Cloud Run). Used by the web UI
    to pick which sync path to call.
    """
    if not bank_key:
        return False
    try:
        import sys as _sys
        if str(ROOT) not in _sys.path:
            _sys.path.insert(0, str(ROOT))
        from bank_fetcher import _load_fetcher_config  # noqa: PLC0415
        from bank_fetcher.credential_manager import (  # noqa: PLC0415
            get_credentials_cloud,
            CredentialNotFoundError,
        )
        import keyring  # noqa: PLC0415

        cfg = _load_fetcher_config(bank_key)
        svc = cfg.get("keyring_service")
        u_key = cfg.get("keyring_username_key")
        p_key = cfg.get("keyring_password_key")
        uid_prefix = f"{get_current_uid()}:" if get_current_uid() else ""
        if svc and u_key and p_key:
            try:
                if keyring.get_password(svc, f"{uid_prefix}{u_key}") and keyring.get_password(svc, f"{uid_prefix}{p_key}"):
                    return True
            except Exception:
                pass
        return get_credentials_cloud(bank_key) is not None
    except Exception:
        return False


def _categorize_scraped(transactions: list[dict], account_type: Optional[str] = None) -> list[dict]:
    """
    Run the full parse_bank merchant-cleaning pipeline on each scraped transaction,
    then categorize using the ML model.
    Mutates each dict in-place, adding 'merchant_clean' and 'category'.

    `account_type` lets the recurrence post-pass skip credit-card accounts
    (where every "credit" is a refund, not income).
    """
    if not transactions:
        return transactions

    # Step 1: clean every merchant name through the same pipeline used for PDF parsing.
    # Pre-strip BofA/ACH metadata (DES:, INDN:, CO ID:, ACH HOLD, etc.) first so
    # the merchant extractor gets a clean base string without unique transaction IDs.
    acct_type_norm = (account_type or "").lower()
    for txn in transactions:
        raw = txn.get("description", "")
        # Pre-process: strip ACH structured metadata before calling merchant_clean
        pre = _normalize_merchant_name(raw) if raw else raw
        try:
            txn["merchant_clean"] = _merchant_clean_fn(pre)
        except Exception:
            txn["merchant_clean"] = pre or raw

        # Step 1b — normalize amount_type via description heuristic.
        # BoA (and other banks) often send rows without a visible negative sign,
        # so content.js can't tell credit from debit reliably and defaults to
        # "debit". That makes Money In always $0 in the UI. Override here when
        # the description clearly indicates money coming IN (DIRECT DEP / PAYROLL
        # / REFUND / ZELLE FROM / etc) so the same fix applied by /recategorize
        # also runs on every fresh ingest.
        original_type = (txn.get("type") or txn.get("amount_type") or "").lower()
        inferred = _infer_amount_type(
            description=str(txn.get("description") or ""),
            merchant_clean=str(txn.get("merchant_clean") or ""),
            category="",  # category not yet assigned at this step
            account_type=acct_type_norm,
        )
        # Only override "debit" → "credit" based on the heuristic; never the
        # other way. If the extension is confident a row is credit (e.g. it
        # parsed a "+" sign), trust it.
        if original_type != "credit" and inferred == "credit":
            txn["type"] = "credit"
            txn["amount_type"] = "credit"

    # Step 2: build categorizer input with cleaned names
    cat_input = [
        {
            **txn,
            "raw_description": txn.get("description", ""),
            "txn_type": "credit" if txn.get("type", "debit") == "credit" else "withdrawals",
            # merchant_clean already set above — categorizer picks it up directly
        }
        for txn in transactions
    ]

    # Step 3: run the 4-layer categorizer (patterns → fuzzy → BERT → rules)
    try:
        categorized = categorize_transactions(cat_input)
        for orig, cat in zip(transactions, categorized):
            orig["category"] = cat.get("category", "Uncategorized")
    except Exception as exc:
        print(f"WARNING: categorization failed: {exc}")

    # Step 4: recurrence post-pass — promotes merchants like "QUALCOMM TECHNOL"
    # whose deposits arrive on a regular cadence to "Income", even when no
    # keyword rule matched. Skipped for credit-card accounts.
    try:
        detect_recurrent_income(transactions, account_type=account_type)
    except Exception as exc:
        print(f"WARNING: recurrence detection failed: {exc}")

    return transactions


def _smart_since_date(account_id: int, fallback_days: int = 365) -> datetime:
    """Return the best since_date for a re-sync of account_id.

    Priority:
      1. Date of the most recently stored transaction minus 1 day (to catch
         same-day transactions and avoid missing anything at the boundary).
      2. Fallback: now - fallback_days (used when the account has no stored
         transactions at all, e.g. first-time connect).

    Using the latest stored transaction date rather than last_synced_at means
    even if auto-sync was failing for months we never re-scrape more than 1
    extra day of already-known transactions.
    """
    latest = get_latest_transaction_date(account_id)
    if latest:
        return latest - timedelta(days=1)
    return datetime.now() - timedelta(days=fallback_days)


def _period_to_since_date(period: str) -> datetime | None:
    """Convert a period string to a cutoff datetime (inclusive lower bound)."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    mapping: dict[str, datetime | None] = {
        "1m":  today - timedelta(days=30),
        "3m":  today - timedelta(days=90),
        "6m":  today - timedelta(days=180),
        "1y":  today - timedelta(days=365),
        "ytd": today.replace(month=1, day=1),
        "all": None,
    }
    return mapping.get(period, mapping["1m"])


# ------------------------------------------------------------------ #
# Smart Connect — server-side automated login state
# ------------------------------------------------------------------ #

# session_id → {status, mfa_queue, result, error, bank_key, account_id}
_smart_sessions: dict = {}


def _smart_connect_worker(
    session_id: str,
    uid: str,
    bank_key: str,
    username: str,
    password: str,
) -> None:
    """Background thread: runs headless Playwright login + scrape for Smart Connect."""
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from bank_fetcher import scrape_automated, _load_fetcher_config

    session = _smart_sessions[session_id]
    uid_token = set_current_uid(uid)

    def mfa_callback() -> str:
        """Block until the user submits the OTP via /api/smart-connect/mfa."""
        session["status"] = "mfa_required"
        try:
            otp = session["mfa_queue"].get(timeout=300)  # 5-minute window
            session["status"] = "scraping"
            return otp
        except _queue.Empty:
            raise RuntimeError("MFA timed out — no code received within 5 minutes.")

    try:
        session["status"] = "logging_in"
        cfg = _load_fetcher_config(bank_key)
        display_name = cfg.get("bank_display_name", bank_key)
        acct_type = cfg.get("account_type", "checking")
        account_id = add_account(bank_key, display_name, sync_period="1y", account_type=acct_type)
        session["account_id"] = account_id

        since_date = datetime.now() - timedelta(days=365)
        transactions, last_url, ending_balance, _ = scrape_automated(
            bank_key=bank_key,
            username=username,
            password=password,
            mfa_callback=mfa_callback,
            since_date=since_date,
        )

        session["status"] = "categorizing"
        _categorize_scraped(transactions, account_type=acct_type)
        store_transactions(account_id, transactions)
        update_last_synced(account_id, datetime.now())
        if last_url:
            set_last_url(account_id, last_url)
        if ending_balance:
            set_account_balance(account_id, ending_balance)
        set_needs_login(account_id, False)

        session["status"] = "done"
        session["result"] = {
            "account":      get_account(account_id),
            "count":        len(transactions),
            "transactions": transactions,
        }
    except Exception as exc:
        session["status"] = "error"
        session["error"] = str(exc)
        print(f"[smart-connect] error in session {session_id}: {exc}", flush=True)
    finally:
        reset_current_uid(uid_token)


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def _add_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _is_public_path(self, path: str) -> bool:
        return path in {
            "/api/version",
            "/api/auth/signup",
            "/api/auth/login",
            "/api/auth/me",
            # Per-bank scrape config (CSS selectors etc.) — same for every
            # user, so no need to auth-gate. The extension's content.js
            # fetches this on every page load, and it has no auth context.
            "/api/extension/scrape-config",
        }

    def _read_bearer_token(self) -> str | None:
        authz = (self.headers.get("Authorization") or "").strip()
        if authz.lower().startswith("bearer "):
            return authz[7:].strip()
        return None

    def _authenticate_request(self, path: str):
        if not path.startswith("/api/") or self._is_public_path(path):
            return None
        token = self._read_bearer_token()
        if not token:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required"})
            return False
        try:
            return verify_token(token)
        except Exception as exc:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
            return False

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _handle_auth_signup(self) -> None:
        try:
            body = self._read_json_body()
            password = body.get("password", "")
            confirm_password = body.get("confirm_password", "")
            if password != confirm_password:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Passwords do not match"})
                return
            user = create_user(
                body.get("first_name", ""),
                body.get("last_name", ""),
                body.get("email", ""),
                password,
            )
            token = issue_token(user["uid"], user["email"])
            self._send_json(HTTPStatus.OK, {"token": token, "user": user})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_auth_login(self) -> None:
        try:
            body = self._read_json_body()
            user = authenticate_user(body.get("email", ""), body.get("password", ""))
            token = issue_token(user["uid"], user["email"])
            self._send_json(HTTPStatus.OK, {"token": token, "user": user})
        except ValueError as exc:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_auth_me(self) -> None:
        token = self._read_bearer_token()
        if not token:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required"})
            return
        try:
            user = verify_token(token)
            self._send_json(HTTPStatus.OK, {"user": user})
        except Exception as exc:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})

    def _handle_auth_delete(self) -> None:
        token = self._read_bearer_token()
        if not token:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required"})
            return
        try:
            user = verify_token(token)
            uid_token = set_current_uid(user["uid"])
            try:
                accounts = get_accounts()
                account_ids = [int(a["id"]) for a in accounts if a.get("id") is not None]
                bank_keys = sorted({str(a.get("bank_key") or "").strip() for a in accounts if a.get("bank_key")})

                # Best-effort local cleanup for keychain and saved browser sessions.
                try:
                    import keyring  # noqa: PLC0415
                    import sys as _sys
                    if str(ROOT) not in _sys.path:
                        _sys.path.insert(0, str(ROOT))
                    from bank_fetcher import _load_fetcher_config  # noqa: PLC0415

                    uid_prefix = f"{user['uid']}:"
                    for bank_key in bank_keys:
                        try:
                            cfg = _load_fetcher_config(bank_key)
                        except Exception:
                            continue
                        svc = cfg.get("keyring_service")
                        for key_field in ("keyring_username_key", "keyring_password_key", "keyring_totp_secret_key"):
                            label = cfg.get(key_field)
                            if svc and label:
                                try:
                                    keyring.delete_password(svc, f"{uid_prefix}{label}")
                                except Exception:
                                    pass
                except Exception:
                    pass

                try:
                    sessions_dir = ROOT / "data" / "sessions"
                    if sessions_dir.exists():
                        for path in sessions_dir.glob(f"{user['uid']}_*.json"):
                            path.unlink(missing_ok=True)
                except Exception:
                    pass

                deleted = delete_user(user["uid"], user.get("email"))
            finally:
                reset_current_uid(uid_token)

            self._send_json(HTTPStatus.OK, {
                "deleted": True,
                "uid": user["uid"],
                "email": user["email"],
                "counts": deleted,
            })
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_auth_password(self) -> None:
        token = self._read_bearer_token()
        if not token:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required"})
            return
        try:
            user = verify_token(token)
            body = self._read_json_body()
            current_password = body.get("current_password", "")
            new_password = body.get("new_password", "")
            if not current_password or not new_password:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "current_password and new_password are required"})
                return
            update_password(user["uid"], current_password, new_password)
            self._send_json(HTTPStatus.OK, {"updated": True})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_auth_profile(self) -> None:
        token = self._read_bearer_token()
        if not token:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required"})
            return
        try:
            user = verify_token(token)
            body = self._read_json_body()
            updated = update_profile(
                user["uid"],
                body.get("first_name", ""),
                body.get("last_name", ""),
            )
            self._send_json(HTTPStatus.OK, {"updated": True, "user": {**user, **updated}})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._add_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def end_headers(self) -> None:
        # Prevent browsers from caching HTML so frontend changes are always fresh.
        if self.path.split("?")[0].endswith(".html") or self.path == "/":
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        auth_user = self._authenticate_request(path)
        if auth_user is False:
            return
        token = set_current_uid(auth_user["uid"]) if isinstance(auth_user, dict) else None
        try:
            if path == "/api/version":
                self._send_json(HTTPStatus.OK, {"version": "v3", "smart_connect": True})
                return
            if path == "/api/auth/me":
                self._handle_auth_me()
                return
            if path == "/api/extension/scrape-config":
                self._handle_extension_scrape_config()
                return
            # Debug endpoint: /api/debug/raw-transactions?bank=icici&limit=10
            if path == "/api/debug/raw-transactions":
                self._handle_debug_raw_transactions()
                return
            if path == "/api/accounts":
                self._handle_get_accounts()
                return
            # /api/accounts/<id>/monthly
            if path.startswith("/api/accounts/") and path.endswith("/monthly"):
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "monthly":
                    try:
                        acct_id = int(parts[2])
                    except ValueError:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid account id"})
                        return
                    self._handle_get_monthly(acct_id)
                    return
            # /api/accounts/<id>/insights
            if path.startswith("/api/accounts/") and path.endswith("/insights"):
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "insights":
                    try:
                        acct_id = int(parts[2])
                    except ValueError:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid account id"})
                        return
                    self._handle_get_insights(acct_id)
                    return
            # /api/accounts/<id>/transactions
            if path.startswith("/api/accounts/") and path.endswith("/transactions"):
                parts = path.strip("/").split("/")  # ['api', 'accounts', '<id>', 'transactions']
                if len(parts) == 4 and parts[3] == "transactions":
                    try:
                        acct_id = int(parts[2])
                    except ValueError:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid account id"})
                        return
                    self._handle_get_transactions(acct_id)
                    return
            if path == "/api/smart-connect/status":
                self._handle_smart_connect_status()
                return
            if path == "/":
                self.path = "/about.html"
            return super().do_GET()
        finally:
            if token is not None:
                reset_current_uid(token)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        auth_user = self._authenticate_request(path)
        if auth_user is False:
            return
        token = set_current_uid(auth_user["uid"]) if isinstance(auth_user, dict) else None
        try:
            if path == "/api/auth/me":
                self._handle_auth_delete()
                return
            if path == "/api/auth/password":
                self._handle_auth_password()
                return
            # DELETE /api/accounts/<id>
            if path.startswith("/api/accounts/"):
                parts = path.strip("/").split("/")
                if len(parts) == 3:
                    try:
                        acct_id = int(parts[2])
                    except ValueError:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid account id"})
                        return
                    self._handle_delete_account(acct_id)
                    return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        finally:
            if token is not None:
                reset_current_uid(token)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        auth_user = self._authenticate_request(path)
        if auth_user is False:
            return
        token = set_current_uid(auth_user["uid"]) if isinstance(auth_user, dict) else None
        try:
            # PATCH /api/accounts/<id>
            if path.startswith("/api/accounts/"):
                parts = path.strip("/").split("/")
                if len(parts) == 3:
                    try:
                        acct_id = int(parts[2])
                    except ValueError:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid account id"})
                        return
                    self._handle_patch_account(acct_id)
                    return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        finally:
            if token is not None:
                reset_current_uid(token)

    def do_OPTIONS(self) -> None:
        # Preflight handler for cross-origin requests.
        self.send_response(HTTPStatus.NO_CONTENT)
        self._add_cors_headers()
        self.end_headers()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        print(f"[POST] raw={self.path!r} parsed={path!r}", flush=True)
        if path == "/api/auth/signup":
            self._handle_auth_signup()
            return
        if path == "/api/auth/login":
            self._handle_auth_login()
            return
        # Cancel in-flight headless scrapes for the current user. Routed BEFORE
        # the normal auth gate because it's typically delivered via
        # navigator.sendBeacon() on window close, which cannot set an
        # Authorization header — so the token rides in the JSON body instead.
        if path == "/api/sync/cancel":
            self._handle_sync_cancel()
            return
        auth_user = self._authenticate_request(path)
        if auth_user is False:
            return
        token = set_current_uid(auth_user["uid"]) if isinstance(auth_user, dict) else None
        try:
            if path == "/api/smart-connect/start":
                self._handle_smart_connect_start()
                return
            if path == "/api/smart-connect/mfa":
                self._handle_smart_connect_mfa()
                return
            if path == "/api/smart-connect/extension-ingest":
                self._handle_smart_connect_extension_ingest()
                return
            if path == "/api/connect-bank":
                self._handle_connect_bank()
                return
            if path == "/api/accounts":
                self._handle_add_account()
                return
            if path == "/api/import":
                self._handle_import_transactions()
                return
            if path == "/api/auth/profile":
                self._handle_auth_profile()
                return
            # POST /api/accounts/<id>/sync
            if path.startswith("/api/accounts/") and path.endswith("/sync"):
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "sync":
                    try:
                        acct_id = int(parts[2])
                    except ValueError:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid account id"})
                        return
                    self._handle_sync_account(acct_id)
                    return
            # POST /api/accounts/<id>/relogin
            if path.startswith("/api/accounts/") and path.endswith("/relogin"):
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "relogin":
                    try:
                        acct_id = int(parts[2])
                    except ValueError:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid account id"})
                        return
                    self._handle_relogin_account(acct_id)
                    return
            # POST /api/accounts/<id>/recategorize
            if path.startswith("/api/accounts/") and path.endswith("/recategorize"):
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "recategorize":
                    try:
                        acct_id = int(parts[2])
                    except ValueError:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid account id"})
                        return
                    self._handle_recategorize_account(acct_id)
                    return
            # POST /api/accounts/<id>/save-credentials
            if path.startswith("/api/accounts/") and path.endswith("/save-credentials"):
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "save-credentials":
                    try:
                        acct_id = int(parts[2])
                    except ValueError:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid account id"})
                        return
                    self._handle_save_credentials(acct_id)
                    return
            # Parse a PDF statement and return structured JSON.
            if path != "/api/parse":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found", "v": "v4", "got": path})
                return

            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Expected multipart/form-data"})
                return

            content_len = int(self.headers.get("Content-Length", "0") or 0)
            if content_len <= 0:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Empty upload"})
                return

            raw_body = self.rfile.read(content_len)
            message = BytesParser(policy=default).parsebytes(
                f"Content-Type: {ctype}\r\n\r\n".encode("utf-8") + raw_body
            )
            # Extract multipart form fields.
            file_part = None
            username = None
            for part in message.iter_parts():
                if part.get_content_disposition() != "form-data":
                    continue
                field_name = part.get_param("name", header="content-disposition")
                if field_name == "file":
                    file_part = part
                elif field_name == "username":
                    username = (part.get_content() or "").strip()
            if file_part is None:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'file' field"})
                return

            file_name = (file_part.get_filename() or "").strip()
            if file_name and not file_name.lower().endswith(".pdf"):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Please upload a PDF file"})
                return

            file_bytes = file_part.get_payload(decode=True) or b""
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = Path(tmp.name)

            try:
                auto_bootstrap = os.environ.get("AUTO_BOOTSTRAP_CONFIG", "1").strip().lower() not in {"0", "false", "no"}
                bootstrap_result = None
                if auto_bootstrap:
                    bootstrap_result = bootstrap_config_if_unknown(tmp_path, config_dir=ROOT / "config" / "banks")

                payload = parse_pdf_to_payload(tmp_path)
                payload["transactions"] = categorize_transactions(payload.get("transactions", []))
                if bootstrap_result:
                    payload["config_bootstrap"] = bootstrap_result
                persist_mode = self._resolve_persist_mode()
                username = username or get_current_uid()
                try:
                    if persist_mode in {"firebase", "firebase_storage", "firestore"}:
                        from cloud_persistence import persist_payload_firebase_storage

                        payload["storage"] = persist_payload_firebase_storage(
                            payload=payload,
                            username=username,
                            source_file=file_name or tmp_path.name,
                            pdf_bytes=file_bytes,
                        )
                    else:
                        payload["storage"] = persist_uploaded_statement(
                            payload=payload,
                            original_filename=file_name or tmp_path.name,
                            pdf_bytes=file_bytes,
                            username=username,
                        )
                except Exception as persist_exc:  # noqa: BLE001
                    print(f"WARNING: persistence failed ({persist_mode}): {persist_exc}")
                    payload["storage"] = {"mode": persist_mode, "error": str(persist_exc)}
                self._log_category_summary(payload["transactions"])
            except Exception as exc:  # noqa: BLE001
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

            self._send_json(HTTPStatus.OK, payload)
        finally:
            if token is not None:
                reset_current_uid(token)

    # ------------------------------------------------------------------ #
    # Extension scrape-config endpoint
    # ------------------------------------------------------------------ #

    def _handle_extension_scrape_config(self) -> None:
        """GET /api/extension/scrape-config?domain=<hostname>
        Returns scraping instructions for a given bank domain so the
        universal Chrome extension content script knows what to do.
        Add new banks here — no extension update needed.
        """
        from urllib.parse import parse_qs, urlparse as _up
        qs = parse_qs(_up(self.path).query)
        domain = qs.get("domain", [""])[0].lower().strip()

        # ── Domain → bank config map ──────────────────────────────────
        CONFIGS = {
            # Capital One
            "myaccounts.capitalone.com": {
                "bank_key": "capital_one",
                "login_check": ["signin", "sign-in", "login"],
                "start_url": "https://myaccounts.capitalone.com/accountSummary",
                # Navigate from account summary → click View More → land on /Card/ page
                "navigate": [
                    {
                        "action": "wait_and_click_text",
                        "texts": ["View More", "View All Transactions", "View Account"],
                        "timeout": 30000,
                        "on_url": "*accountSummary*",
                    }
                ],
                "ready_selector": "c1-ease-row.c1-ease-table__row",
                "ready_url_pattern": "/Card/",
                "ready_timeout": 60000,
                "rows": {
                    "selector": "c1-ease-row.c1-ease-table__row",
                    "date":        ".cdk-column-date",
                    "description": ".cdk-column-description",
                    "amount":      ".cdk-column-amount",
                    "amount_sign": "positive_is_debit",
                },
                "balance_selectors": [
                    ".balance-amount", ".current-balance",
                    "[data-testid*='balance']", ".c1-ease-account-summary__balance",
                ],
                "pagination": None,
            },

            # ICICI Bank
            "retailnetbanking.icici.bank.in": {
                "bank_key": "icici",
                # ICICI redirects to several different URLs on session expiry.
                # content.js also runs DOM-based heuristics (password input,
                # "session expired" text) when none of these match.
                "login_check": [
                    "login-page", "/login", "logout",
                    "session", "logon", "signin", "sign-in",
                    "AuthenticationController", "authForm",
                ],
                "start_url": None,   # uses last_url from account record
                "navigate": [],      # rely on last_url pointing directly to statement page
                "ready_selector": "tr.transfer:not(.trans_space)",
                "ready_url_pattern": None,
                "ready_timeout": 45000,
                "rows": {
                    "selector":    "tr.transfer:not(.trans_space)",
                    "description": "td.remarks-details p.name, td.remarks-details p.remark-pos",
                    "credit":      "td.credit p, td.cr-mob p",
                    "debit":       "td.debit p",
                    "balance":     "td.balance p",
                    "date_mode":   "parent_walk",   # date lives in ancestor element
                    "amount_sign": "credit_debit_cols",
                },
                "balance_selectors": [
                    "td.balance p",   # first row's balance = current balance
                ],
                "pagination": {
                    "type": "next_button",
                    "button_texts": [">", "›", "❯"],
                    "page_info_pattern": r"page\s+\d+\s+of\s+\d+",
                },
            },

            # Bank of America
            "www.bankofamerica.com": {
                "bank_key": "boa",
                "login_check": ["sign-in", "signin", "signon", "login"],
                # BOA serves the accounts overview at signIn.go?torLsLogin=true — the URL
                # contains "signin" but the user IS logged in. Presence of the username nav
                # element overrides the login_check so content.js doesn't bail early.
                "logged_in_selector": ".fsdnav-username, tr.activity-row",
                "start_url": None,   # uses last_url from account record
                # Auto-click the first account link from the accounts overview.
                # - on_url: "myaccounts" — only run on overview URLs (signIn.go,
                #   /myaccounts/overview, etc.). This prevents the step from
                #   firing on the activity page itself, where the page footer
                #   has a language-switcher link that ALSO points to
                #   /deposit-details/activity/... and would cause an
                #   English↔Spanish redirect loop.
                # - The "/?" suffix in the selector matches the bare activity
                #   URL only; locale variants like /activity/es/? are excluded.
                # Skip ALL automation when BoA returns an error page (e.g.
                # accounts-overview.go?msgId=MSGOBOError). Otherwise we'd loop
                # the error page indefinitely and dig the lockout hole deeper.
                "skip_on_url_contains": ["MSGOBOError", "msgId=MSGOBO"],
                # MSGOBOError is often a benign warning box on a fully usable
                # overview page. Only honor skip_on_url_contains when NONE of
                # these "page is usable" markers are present. If the real account
                # links (or activity rows) exist, the page works — proceed and
                # click, exactly as the user does by hand.
                "skip_override_selector": (
                    ".AccountItemDeposit a[href*='redirect.go'],"
                    ".AccountItem a[href*='redirect.go?source=overview'],"
                    "a[href*='/myaccounts/brain/redirect.go?source=overview'],"
                    "tr.activity-row"
                ),
                "navigate": [
                    {
                        "action": "wait_and_click_selector",
                        "on_url": "myaccounts",
                        # MUST require redirect.go in the href — anything
                        # broader (tr.single-account a, a.AccountItem) ends up
                        # matching BoA's "Continue online banking session"
                        # button on the session-error page, which redirects
                        # back to the error page and creates a refresh loop.
                        "selector": (
                            ".AccountItemDeposit a[href*='redirect.go'],"
                            ".AccountItem a[href*='redirect.go?source=overview'],"
                            "a[href*='/myaccounts/brain/redirect.go?source=overview']"
                        ),
                        "skip_if_selector_present": (
                            "tr.activity-row,"
                            "[data-testid='transaction-row'],"
                            "bofa-trans-row,"
                            "tr[data-row-key]"
                        ),
                        # The MSGOBOError page renders a "Continue" button that
                        # ALSO matches the redirect.go selector and sits above the
                        # account list, so a naive querySelector clicks it and
                        # loops back to the error page. Skip it by text.
                        # NOTE: don't skip bare "rewards" by text — a real
                        # "Cash Rewards Visa" account contains that word. The
                        # rewards *page* is excluded by href below instead.
                        "click_skip_text": ["continue", "view rewards", "merrill", "open an account", "apply now"],
                        # Promotional / cross-sell links share the same
                        # redirect.go?source=overview href shape as the real
                        # account link. Exclude them by href substring so the
                        # picker can't wander into the Rewards or Merrill pages.
                        "click_skip_href": [
                            "rewards", "customer-rewards", "myrewards",
                            "merrill", "deals", "special", "offer",
                            "enroll", "/apply", "advertise",
                        ],
                        # Positively target the account link. BoA labels it with
                        # the account type ("Adv Plus Banking - 7238",
                        # "Customized Cash Rewards Visa - 1234", etc.); these
                        # keywords + the masked account-number digits give it a
                        # high score so it wins over any remaining decoy.
                        "click_prefer_text": [
                            "banking", "checking", "savings", "money market",
                            "credit", "card", "visa", "mastercard",
                            "advantage", "adv plus", "cash rewards", "platinum",
                        ],
                        "timeout": 30000,
                    }
                ],
                "first_connect_hint": "You're in! FIFO will open your account in a moment — please don't navigate away.",
                # Stop paginating once the oldest visible row is older than this
                # many days. Keeps first-connect scrapes scoped to ~1 year and
                # prevents the loop from grinding through 5+ years of history.
                "stop_after_days": 365,
                # Several selectors — BoA renames classes over time. querySelector
                # picks the first that matches. content.js also logs per-candidate
                # counts so we can see what the page actually uses.
                "ready_selector": (
                    "tr.activity-row,"
                    "[data-testid='transaction-row'],"
                    "bofa-trans-row,"
                    "tr[data-row-key],"
                    ".trans-row,"
                    "tr[id^='trans-']"
                ),
                "ready_url_pattern": None,
                "ready_timeout": 300000,
                "rows": {
                    "selector":    (
                        "tr.activity-row,"
                        "[data-testid='transaction-row'],"
                        "bofa-trans-row,"
                        "tr[data-row-key],"
                        ".trans-row,"
                        "tr[id^='trans-']"
                    ),
                    "date":        "td:nth-child(1)",
                    "description": "td:nth-child(2)",
                    "amount_type": "td:nth-child(3)",
                    "amount":      "td:nth-child(4)",
                    "balance":     "td:nth-child(5)",
                    "amount_sign": "type_column",
                },
                "balance_selectors": [
                    ".available-balance-amount", ".current-balance-amount",
                    "[data-testid*='balance']", ".balance-amount",
                ],
                "pagination": {
                    "type": "view_more_button",
                    "button_texts": [
                        "view more transactions", "view more", "show more", "load more"
                    ],
                },
            },

            # Discover (future — add content when ready)
            "card.discover.com": {
                "bank_key": "discover",
                "login_check": ["signin", "login"],
                "start_url": None,
                "navigate": [],
                "ready_selector": ".transaction-item, tr.trans-row",
                "ready_timeout": 45000,
                "rows": {
                    "selector":    ".transaction-item, tr.trans-row",
                    "date":        ".trans-date, td:nth-child(1)",
                    "description": ".trans-merchant, td:nth-child(2)",
                    "amount":      ".trans-amount, td:nth-child(3)",
                    "amount_sign": "positive_is_debit",
                },
                "balance_selectors": [".account-balance", ".current-balance"],
                "pagination": None,
            },
        }

        # Direct lookup first, then try suffix matching for subdomains
        # e.g. secure.bankofamerica.com → matches www.bankofamerica.com config
        cfg = CONFIGS.get(domain)
        if not cfg:
            for key, val in CONFIGS.items():
                base = key.lstrip("www.")
                if domain.endswith(base):
                    cfg = val
                    break
        if not cfg:
            self._send_json(HTTPStatus.NOT_FOUND, {
                "error": f"No scrape config for domain: {domain}",
                "supported": list(CONFIGS.keys()),
            })
            return

        self._send_json(HTTPStatus.OK, cfg)

    # ------------------------------------------------------------------ #
    # Smart Connect endpoints
    # ------------------------------------------------------------------ #

    def _handle_smart_connect_start(self) -> None:
        """POST /api/smart-connect/start — kick off automated headless login."""
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        bank_key = body.get("bank_key", "").strip()
        username  = body.get("username", "").strip()
        password  = body.get("password", "")

        if not bank_key or not username or not password:
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"error": "bank_key, username, and password are required"})
            return

        session_id = str(_uuid.uuid4())
        _smart_sessions[session_id] = {
            "status":    "starting",
            "mfa_queue": _queue.Queue(),
            "result":    None,
            "error":     None,
            "account_id": None,
            "bank_key":  bank_key,
        }

        threading.Thread(
            target=_smart_connect_worker,
            args=(session_id, get_current_uid(), bank_key, username, password),
            daemon=True,
            name=f"smart-connect-{session_id[:8]}",
        ).start()

        self._send_json(HTTPStatus.OK, {"session_id": session_id, "status": "starting"})

    def _handle_smart_connect_mfa(self) -> None:
        """POST /api/smart-connect/mfa — deliver OTP to the waiting browser thread."""
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        session_id = body.get("session_id", "")
        otp = body.get("otp", "").strip()

        session = _smart_sessions.get(session_id)
        if not session:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Session not found"})
            return
        if not otp:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "otp is required"})
            return

        session["mfa_queue"].put(otp)
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def _handle_smart_connect_status(self) -> None:
        """GET /api/smart-connect/status?session_id=<id> — poll for progress."""
        qs = parse_qs(urlparse(self.path).query)
        session_id = qs.get("session_id", [""])[0]

        session = _smart_sessions.get(session_id)
        if not session:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Session not found"})
            return

        resp: dict = {"status": session["status"]}
        if session.get("error"):
            resp["error"] = session["error"]
        if session.get("result"):
            resp.update(session["result"])
        self._send_json(HTTPStatus.OK, resp)

    def _handle_smart_connect_extension_ingest(self) -> None:
        """POST /api/smart-connect/extension-ingest

        Accepts transactions scraped by the FIFO Chrome extension running in the
        user's own browser.  No Playwright / headless Chromium involved — the
        user logs in themselves so there is zero bot-detection risk.

        Body JSON:
          {
            "bank_key":       "boa",
            "transactions":   [...],        # list of {date, description, amount, ...}
            "ending_balance": "1234.56"     # optional
          }
        """
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        bank_key = body.get("bank_key", "").strip()
        transactions = body.get("transactions", [])
        ending_balance = body.get("ending_balance")
        last_url = body.get("last_url", "").strip() or None

        if not bank_key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bank_key is required"})
            return
        if not isinstance(transactions, list):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "transactions must be a list"})
            return

        try:
            import sys
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from bank_fetcher import _load_fetcher_config

            cfg = _load_fetcher_config(bank_key)
            display_name = cfg.get("bank_display_name", bank_key)
            acct_type = cfg.get("account_type", "checking")

            account_id = add_account(bank_key, display_name, sync_period="1y", account_type=acct_type)
            _categorize_scraped(transactions, account_type=acct_type)
            # store_transactions returns the count of *newly inserted* rows
            # (existing rows are dedup'd). Report THAT, not len(transactions),
            # so the UI says "5 new" after a delta sync instead of "1180 imported"
            # when 1175 were duplicates.
            new_count = store_transactions(account_id, transactions)
            update_last_synced(account_id, datetime.now())
            if ending_balance:
                set_account_balance(account_id, ending_balance)
            if last_url:
                set_last_url(account_id, last_url)
            set_needs_login(account_id, False)
            set_account_status(account_id, "connected")

            print(
                f"[extension-ingest] {bank_key}: stored {new_count} new / {len(transactions)} sent "
                f"for account_id={account_id}",
                flush=True,
            )

            self._send_json(HTTPStatus.OK, {
                "account":      get_account(account_id),
                "count":        new_count,
                "received":     len(transactions),
                "transactions": transactions,
            })
        except Exception as exc:
            print(f"[extension-ingest] error: {exc}", flush=True)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_connect_bank(self) -> None:
        """POST /api/connect-bank — open browser, log in, return transactions for requested period."""
        import sys as _sys
        _proj_root = str(ROOT)
        if _proj_root not in _sys.path:
            _sys.path.insert(0, _proj_root)
        from bank_fetcher import scrape_recent_transactions, _load_fetcher_config

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        bank_key = body.get("bank", "boa")
        period = body.get("period", "1y")
        since_date = _period_to_since_date(period)

        try:
            cfg = _load_fetcher_config(bank_key)
            transactions, _ = scrape_recent_transactions(bank_key=bank_key, since_date=since_date)
            self._send_json(HTTPStatus.OK, {
                "bank": cfg.get("bank_display_name", bank_key),
                "period": period,
                "count": len(transactions),
                "transactions": transactions,
            })
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    # ------------------------------------------------------------------ #
    # Account management endpoints
    # ------------------------------------------------------------------ #

    def _handle_get_accounts(self) -> None:
        """GET /api/accounts — list all connected accounts.

        The `has_credentials` flag is read from the account doc itself (set
        when the user saves a password). We deliberately do NOT probe the OS
        keychain here, because each keychain access triggers a macOS auth
        prompt — spamming the user.
        """
        accounts = get_accounts()
        for acct in accounts:
            acct.setdefault("has_credentials", False)
        self._send_json(HTTPStatus.OK, accounts)

    def _handle_debug_raw_transactions(self) -> None:
        """GET /api/debug/raw-transactions?bank=<key>&limit=<n>
        Returns raw Firestore transaction data for the named bank, useful for diagnosing
        date/amount storage issues without needing firebase-admin directly.
        """
        qs = parse_qs(urlparse(self.path).query)
        bank_key = qs.get("bank", ["icici"])[0]
        limit = int(qs.get("limit", ["20"])[0])
        accounts = get_accounts()
        acct = next((a for a in accounts if a.get("bank_key") == bank_key), None)
        if not acct:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"No account with bank_key={bank_key!r}"})
            return
        aid = acct["id"]
        txns = get_transactions(aid)
        sample = txns[:limit]
        summary = {
            "account_id": aid,
            "bank_key": bank_key,
            "total_transactions": len(txns),
            "null_dates": sum(1 for t in txns if not t.get("date")),
            "iso_dates": sum(1 for t in txns if (t.get("date") or "").count("-") == 2),
            "other_dates": sum(1 for t in txns if t.get("date") and (t.get("date") or "").count("-") != 2),
            "sample": [
                {
                    "date": t.get("date"),
                    "description": (t.get("description") or "")[:50],
                    "amount": t.get("amount"),
                    "amount_type": t.get("amount_type"),
                }
                for t in sample
            ],
        }
        self._send_json(HTTPStatus.OK, summary)

    def _handle_get_transactions(self, account_id: int) -> None:
        """GET /api/accounts/<id>/transactions?period= — return stored transactions."""
        if get_account(account_id) is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Account not found"})
            return
        qs = parse_qs(urlparse(self.path).query)
        period = qs.get("period", ["all"])[0]
        since_date = _period_to_since_date(period)
        txns = get_transactions(account_id, since_date=since_date)
        # Ensure frontend field names are available regardless of how the transaction was stored.
        # Firestore stores "amount_type" (from scraper); frontend JS expects "amount_type" directly
        # but also reads t.txn_type in some paths — keep both in sync.
        for t in txns:
            if "amount_type" not in t and "type" in t:
                t["amount_type"] = t["type"]
        self._send_json(HTTPStatus.OK, txns)

    def _handle_get_insights(self, account_id: int) -> None:
        """GET /api/accounts/<id>/insights?period= — category totals, income, expenses."""
        if get_account(account_id) is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Account not found"})
            return
        qs = parse_qs(urlparse(self.path).query)
        period = qs.get("period", ["all"])[0]
        since_date = _period_to_since_date(period)
        txns = get_transactions(account_id, since_date=since_date)

        total_income = 0.0
        total_expenses = 0.0
        by_category: dict[str, dict] = {}

        _TRANSFER_CATS = {"Transfer", "Debt Payments"}
        for t in txns:
            amt = _parse_amount(t.get("amount"))
            if amt is None:
                continue
            cat = t.get("category") or "Uncategorized"
            # Exclude internal transfers from income/expense totals to avoid double-counting
            if cat in _TRANSFER_CATS:
                continue
            atype = t.get("amount_type") or t.get("type") or "debit"
            if atype == "credit":
                total_income += abs(amt)
            else:
                total_expenses += abs(amt)
            if cat not in by_category:
                by_category[cat] = {"category": cat, "total": 0.0, "count": 0}
            by_category[cat]["total"] = round(by_category[cat]["total"] + abs(amt), 2)
            by_category[cat]["count"] += 1

        # Top merchants by expense amount (exclude transfers)
        merchant_totals: dict[str, float] = {}
        for t in txns:
            amt = _parse_amount(t.get("amount"))
            if amt is None:
                continue
            cat = t.get("category") or "Uncategorized"
            if cat in _TRANSFER_CATS:
                continue
            atype = t.get("amount_type") or t.get("type") or "debit"
            if atype != "credit":
                raw_name = t.get("merchant_clean") or t.get("description") or "Unknown"
                name = _normalize_merchant_name(raw_name)
                key = name.lower()   # case-insensitive grouping key
                if key not in merchant_totals:
                    merchant_totals[key] = {"name": name, "total": 0.0}
                # Prefer the shorter/cleaner display name (e.g. merchant_clean over raw desc)
                if t.get("merchant_clean"):
                    clean = _normalize_merchant_name(t["merchant_clean"])
                    if len(clean) < len(merchant_totals[key]["name"]):
                        merchant_totals[key]["name"] = clean
                merchant_totals[key]["total"] = round(merchant_totals[key]["total"] + abs(amt), 2)
        top_merchants = sorted(
            [{"name": v["name"], "total": v["total"]} for v in merchant_totals.values()],
            key=lambda x: x["total"], reverse=True
        )[:10]

        account = get_account(account_id)
        current_balance = account.get("account_balance", "") if account else ""
        acct_type = (account.get("account_type") or "").lower() if account else ""
        # Fallback: infer account_type from the bank's JSON config file for accounts
        # that were connected before this field was added to the DB schema.
        # Read the JSON directly — no bank_fetcher import needed.
        if not acct_type and account:
            try:
                _bk = account.get("bank_key", "")
                _cfg_path = ROOT / "bank_fetcher" / "configs" / f"{_bk}_fetcher_config.json"
                if _cfg_path.exists():
                    import json as _json
                    _cfg = _json.loads(_cfg_path.read_text())
                    acct_type = (_cfg.get("account_type") or "checking").lower()
            except Exception:
                pass
        if not acct_type:
            acct_type = "checking"

        # For credit cards: charges = expenses, payments = income, unpaid = charges - payments
        total_charges  = round(total_expenses, 2) if acct_type == "credit" else 0.0
        total_payments = round(total_income, 2)   if acct_type == "credit" else 0.0
        unpaid_charges = round(max(0.0, total_charges - total_payments), 2)

        self._send_json(HTTPStatus.OK, {
            "account_type":   acct_type,
            "total_income":   round(total_income, 2),
            "total_expenses": round(total_expenses, 2),
            "net":            round(total_income - total_expenses, 2),
            "current_balance": current_balance,
            "total_charges":  total_charges,
            "total_payments": total_payments,
            "unpaid_charges": unpaid_charges,
            "by_category": sorted(by_category.values(), key=lambda x: x["total"]),
            "top_merchants": top_merchants,
        })

    def _handle_get_monthly(self, account_id: int) -> None:
        """GET /api/accounts/<id>/monthly?months=12 — expenses/income grouped by YYYY-MM."""
        if get_account(account_id) is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Account not found"})
            return
        qs = parse_qs(urlparse(self.path).query)
        months = max(1, min(60, int(qs.get("months", ["12"])[0])))
        since_date = (
            datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=30 * months)
        )
        txns = get_transactions(account_id, since_date=since_date)

        by_month: dict[str, dict] = {}
        for t in txns:
            raw_date = (t.get("date") or "").strip()
            try:
                dt = datetime.strptime(raw_date, "%Y-%m-%d")
            except ValueError:
                try:
                    dt = datetime.strptime(raw_date, "%m/%d/%Y")
                except ValueError:
                    continue
            key = dt.strftime("%Y-%m")
            if key not in by_month:
                by_month[key] = {"month": key, "expenses": 0.0, "income": 0.0}
            amt = _parse_amount(t.get("amount"))
            if amt is None:
                continue
            # Skip transfers to avoid double-counting in monthly chart
            cat = t.get("category") or "Uncategorized"
            if cat in ("Transfer", "Debt Payments"):
                continue
            atype = (t.get("amount_type") or t.get("type") or "debit").lower()
            if atype == "credit":
                by_month[key]["income"] = round(by_month[key]["income"] + abs(amt), 2)
            else:
                by_month[key]["expenses"] = round(by_month[key]["expenses"] + abs(amt), 2)

        self._send_json(HTTPStatus.OK, {
            "months": sorted(by_month.values(), key=lambda x: x["month"])
        })

    def _handle_add_account(self) -> None:
        """POST /api/accounts — connect a new bank account, scrape, store.

        If username + password are provided in the request body, saves them to the
        OS keychain / Firestore and uses fully headless automated login (no browser
        window). Otherwise falls back to the interactive browser login flow.
        """
        import sys as _sys
        _proj_root = str(ROOT)
        if _proj_root not in _sys.path:
            _sys.path.insert(0, _proj_root)
        from bank_fetcher import scrape_recent_transactions, _load_fetcher_config
        from bank_fetcher.credential_manager import store_credentials_cloud

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        bank_key  = body.get("bank", "boa")
        period    = body.get("period", "1y")
        username  = (body.get("username") or "").strip()
        password  = body.get("password") or ""
        since_date = _period_to_since_date(period)

        try:
            cfg          = _load_fetcher_config(bank_key)
            display_name = cfg.get("bank_display_name", bank_key)
            acct_type    = cfg.get("account_type", "checking")
            account_id   = add_account(bank_key, display_name, sync_period=period, account_type=acct_type)
            uid = get_current_uid() or "anon"
            session_path = ROOT / "data" / "sessions" / f"{uid}_{account_id}.json"

            if username and password and cfg.get("login_steps"):
                # ── Credential-based headless connect ────────────────────────
                # Save credentials to keychain so every future sync is automatic.
                store_credentials_cloud(bank_key, username, password)
                print(f"[connect] Credentials saved for {display_name}", flush=True)

                from bank_fetcher.base_crawler import BankCrawler
                from bank_fetcher import DEFAULT_RAW_DIR
                crawler = BankCrawler(fetcher_cfg=cfg, raw_dir=DEFAULT_RAW_DIR)
                records, last_url = crawler.run_scrape_automated(
                    username=username,
                    password=password,
                    since_date=since_date,
                    session_save_path=session_path,
                )
                transactions = [
                    {"date": r.date, "description": r.description,
                     "amount": r.amount, "type": r.amount_type}
                    for r in records
                ]
                ending_balance   = crawler._ending_balance
                starting_balance = crawler._starting_balance
                extra_vals       = crawler._extra_values
            else:
                # ── Interactive browser login (fallback) ─────────────────────
                transactions, last_url, ending_balance, starting_balance, extra_vals = scrape_recent_transactions(
                    bank_key=bank_key, since_date=since_date, session_save_path=session_path
                )

            _categorize_scraped(transactions)
            store_transactions(account_id, transactions)
            update_last_synced(account_id, datetime.now())
            if last_url:
                set_last_url(account_id, last_url)
            if ending_balance:
                set_account_balance(account_id, ending_balance)
            if inv_bal := (extra_vals or {}).get("investment_balance", ""):
                set_investment_balance(account_id, inv_bal)
            set_needs_login(account_id, False)
            account = get_account(account_id)
            self._send_json(HTTPStatus.OK, {
                "account":          account,
                "period":           period,
                "count":            len(transactions),
                "transactions":     transactions,
                "ending_balance":   ending_balance,
                "starting_balance": starting_balance,
            })
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_sync_cancel(self) -> None:
        """
        POST /api/sync/cancel — abort the current user's in-flight headless
        scrapes. Triggered when the user closes the FIFO window (sent via
        navigator.sendBeacon, so auth rides in the body, not a header).

        Body: {} (token from Authorization header) or {"token": "<jwt>"}.
        Only ever signals cancellation — never an OTP/re-login condition.
        """
        # Resolve uid from the bearer header (normal fetch) OR a body token
        # (sendBeacon can't set headers).
        token = self._read_bearer_token()
        body = {}
        if not token:
            try:
                body = self._read_json_body()
            except Exception:
                body = {}
            token = (body.get("token") or "").strip() if isinstance(body, dict) else ""
        if not token:
            # No identity → nothing we can safely cancel. Beacons are fire-and-
            # forget, so just acknowledge without erroring.
            self._send_json(HTTPStatus.OK, {"cancelled": 0})
            return
        try:
            user = verify_token(token)
        except Exception:
            self._send_json(HTTPStatus.OK, {"cancelled": 0})
            return
        n = _cancel_scrapes(user.get("uid", ""))
        if n:
            print(f"SYNC CANCEL: signalled {n} active scrape(s) for uid={user.get('uid')}", flush=True)
        self._send_json(HTTPStatus.OK, {"cancelled": n})

    def _handle_sync_account(self, account_id: int) -> None:
        """
        POST /api/accounts/<id>/sync — fully headless background sync. No window opens.

        Priority:
          1. Saved credentials in keychain → fresh headless login every time (most reliable,
             never expires, works as long as password hasn't changed).
          2. Saved session cookies → fast replay (fallback if no credentials stored yet).
          3. Neither available → return 422 so frontend prompts Re-login.
        """
        import sys as _sys
        _proj_root = str(ROOT)
        if _proj_root not in _sys.path:
            _sys.path.insert(0, _proj_root)
        from bank_fetcher import scrape_recent_transactions_headless, _load_fetcher_config
        from bank_fetcher.base_crawler import BankCrawler, SessionExpiredError, MfaRequiredError, ScrapeCancelled
        from bank_fetcher import DEFAULT_RAW_DIR
        from bank_fetcher.credential_manager import get_credentials_cloud, CredentialNotFoundError

        account = get_account(account_id)
        if account is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Account not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}
        period       = account.get("sync_period") or "1y"
        since_date   = _smart_since_date(account_id)
        bank_key     = account["bank_key"]
        uid = get_current_uid() or "anon"
        session_path = ROOT / "data" / "sessions" / f"{uid}_{account_id}.json"
        last_url      = account.get("last_url")
        txns          = None
        ending_balance = ""
        # Track WHY the credential login didn't produce transactions so we only
        # ever prompt re-login when it's genuinely warranted (real OTP challenge
        # or expired session) — never for a technical failure (stale selector,
        # navigation timeout, bot block). This is the anti-false-alarm guard.
        mfa_required = False     # bank GENUINELY showed an OTP challenge we can't auto-solve
        tech_error   = None      # non-OTP failure — must NOT masquerade as "re-login required"
        cancelled    = False     # user closed the FIFO window → benign abort, NOT an error

        # ── Path 1: credentials in keychain → fresh headless login ───────────
        try:
            cfg   = _load_fetcher_config(bank_key)
            # get_credentials() tries OS keychain first, then Firestore.
            # Previously this was get_credentials_cloud() which only checked
            # Firestore — so credentials saved via the UI (which writes to
            # Keychain locally) were invisible to the sync.
            from bank_fetcher.credential_manager import (  # noqa: PLC0415
                get_credentials,
                CredentialNotFoundError,
            )
            try:
                creds = get_credentials(cfg)
            except CredentialNotFoundError:
                creds = None
            if creds and cfg.get("login_steps"):
                crawler = BankCrawler(fetcher_cfg=cfg, raw_dir=DEFAULT_RAW_DIR)
                _register_scrape(uid, crawler)   # so window-close can cancel it
                try:
                    records, new_last_url = crawler.run_scrape_automated(
                        username=creds.username,
                        password=creds.password,
                        since_date=since_date,
                        session_save_path=session_path,
                    )
                finally:
                    _unregister_scrape(uid, crawler)
                txns = [{"date": r.date, "description": r.description,
                         "amount": r.amount, "type": r.amount_type} for r in records]
                ending_balance = crawler._ending_balance or ""
                if new_last_url:
                    set_last_url(account_id, new_last_url)
                print(f"SYNC (credentials): {account['display_name']} — {len(txns)} transactions", flush=True)
        except ScrapeCancelled as cancel_err:
            # User closed the FIFO window mid-sync. Benign — abort quietly and
            # never touch needs_login or claim OTP.
            cancelled = True
            print(f"SYNC cancelled (credential path): {cancel_err}", flush=True)
        except MfaRequiredError as mfa_err:
            # GENUINE OTP challenge. Don't give up yet — a trusted saved session
            # (Path 2) may still sync without any OTP. Remember it so we only
            # prompt re-login if Path 2 also can't proceed.
            mfa_required = True
            print(f"SYNC: OTP genuinely required on credential login ({mfa_err}); trying session cookies…", flush=True)
        except Exception as cred_err:
            # NOT an OTP issue — a real/technical failure (e.g. stale login
            # selector, navigation timeout, bot block). NEVER surface this as
            # "OTP/re-login required". Try session cookies; if that also fails
            # we report a plain technical error without setting needs_login.
            tech_error = str(cred_err)
            print(f"SYNC credential path failed ({cred_err}), trying session cookies…", flush=True)

        # ── Cancelled mid-flight (window closed)? Abort quietly. ─────────────
        # 409 Conflict, reason="cancelled" — NOT 422 (re-login) or 500 (error),
        # so the extension treats it as a benign skip and never false-alarms.
        if cancelled:
            self._send_json(HTTPStatus.CONFLICT, {
                "error": "Sync cancelled (window closed).",
                "reason": "cancelled",
            })
            return

        # ── Path 2: session cookies fallback ─────────────────────────────────
        if txns is None:
            if not session_path.exists() or not last_url:
                # No trusted session to fall back on — pick the HONEST reason:
                if mfa_required:
                    # Real OTP challenge → manual re-login is the correct ask.
                    set_needs_login(account_id, True)
                    self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {
                        "error": "Your bank asked for a one-time verification code. Please click ↩ Re-login to verify.",
                        "reason": "otp_required",
                    })
                    return
                if tech_error is not None:
                    # Technical failure → do NOT claim OTP/re-login and do NOT
                    # set needs_login. Surface a plain error so we can fix the
                    # cause instead of nagging the user to re-login.
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                        "error": f"Sync couldn't complete (not an OTP issue): {tech_error}",
                        "reason": "sync_error",
                    })
                    return
                # Genuinely nothing to sync with: no saved credentials AND no session.
                set_needs_login(account_id, True)
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {
                    "error": "No saved login or session yet — please click ↩ Re-login to connect.",
                    "reason": "no_session",
                })
                return
            _replay_crawler = {}   # holds the crawler so we can unregister it
            def _capture(c):
                _replay_crawler["c"] = c
                _register_scrape(uid, c)
            try:
                txns = scrape_recent_transactions_headless(
                    bank_key=bank_key,
                    since_date=since_date,
                    session_path=session_path,
                    account_url=last_url,
                    # Register the replay crawler too so window-close cancels it.
                    on_crawler=_capture,
                )
                print(f"SYNC (session): {account['display_name']} — {len(txns)} transactions", flush=True)
            except ScrapeCancelled as cancel_err:
                # User closed the FIFO window mid-replay. Benign abort.
                print(f"SYNC cancelled (session path): {cancel_err}", flush=True)
                self._send_json(HTTPStatus.CONFLICT, {
                    "error": "Sync cancelled (window closed).",
                    "reason": "cancelled",
                })
                return
            except SessionExpiredError as err:
                print(f"SYNC session expired for {account['display_name']}: {err}", flush=True)
                set_needs_login(account_id, True)
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {
                    "error": "Session expired — please click ↩ Re-login to authenticate again.",
                    "reason": "session_expired",
                })
                return
            except Exception as exc:
                # Technical error during session replay — not an OTP/re-login case.
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                    "error": str(exc), "reason": "sync_error",
                })
                return
            finally:
                if _replay_crawler.get("c") is not None:
                    _unregister_scrape(uid, _replay_crawler["c"])

        _categorize_scraped(txns)
        store_transactions(account_id, txns)
        update_last_synced(account_id, datetime.now())
        if ending_balance:
            set_account_balance(account_id, ending_balance)
        set_needs_login(account_id, False)
        self._send_json(HTTPStatus.OK, {
            "account":      get_account(account_id),
            "period":       period,
            "count":        len(txns),
            "transactions": txns,
        })

    def _handle_relogin_account(self, account_id: int) -> None:
        """
        POST /api/accounts/<id>/relogin — interactive re-login for a specific account.
        Opens a browser window for the user to log in + complete OTP, then fetches the
        full history based on the account's sync_period (not delta). Saves the new session
        so future Sync Now (headless) can use it. Never creates a duplicate account row.
        """
        import sys as _sys
        _proj_root = str(ROOT)
        if _proj_root not in _sys.path:
            _sys.path.insert(0, _proj_root)
        from bank_fetcher import scrape_recent_transactions
        from db import set_account_type as _set_account_type

        account = get_account(account_id)
        if account is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Account not found"})
            return

        # Re-login = session cookie refresh + delta sync (same as Sync Now).
        # Full history was already fetched on first connect; no need to re-scrape it.
        # _smart_since_date returns latest stored transaction − 1 day, or a full
        # sync_period fallback only when there are zero stored transactions yet.
        _period_map = {"1m": 30, "3m": 90, "6m": 180, "1y": 365, "ytd": 365, "all": 730}
        sync_period = account.get("sync_period") or "1y"
        fallback_days = _period_map.get(sync_period, 365)
        since_date = _smart_since_date(account_id, fallback_days=fallback_days)

        # Auto-heal: ensure DB account_type matches the fetcher config (e.g. "credit" for Capital One).
        # Fixes accounts created before account_type support was added.
        try:
            _cfg = _load_fetcher_config(account["bank_key"])
            _cfg_type = _cfg.get("account_type", "")
            if _cfg_type and account.get("account_type") != _cfg_type:
                _set_account_type(account_id, _cfg_type)
                print(f"[relogin] Fixed account_type for {account['display_name']}: "
                      f"{account.get('account_type')!r} → {_cfg_type!r}", flush=True)
        except Exception:
            pass  # Non-fatal — don't block the relogin if config read fails

        try:
            uid = get_current_uid() or "anon"
            session_path = ROOT / "data" / "sessions" / f"{uid}_{account_id}.json"
            transactions, last_url, ending_balance, starting_balance, extra_vals = scrape_recent_transactions(
                bank_key=account["bank_key"],
                since_date=since_date,
                session_save_path=session_path,
            )
            _categorize_scraped(transactions)
            new_count = store_transactions(account_id, transactions)
            update_last_synced(account_id, datetime.now())
            if last_url:
                set_last_url(account_id, last_url)
            if ending_balance:
                set_account_balance(account_id, ending_balance)
            if inv_bal := extra_vals.get("investment_balance", ""):
                set_investment_balance(account_id, inv_bal)
            set_needs_login(account_id, False)
            self._send_json(HTTPStatus.OK, {
                "account":   get_account(account_id),
                "count":     new_count,
                "transactions": transactions,
            })
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_save_credentials(self, account_id: int) -> None:
        """POST /api/accounts/<id>/save-credentials — store bank credentials.

        Body: { "username": "...", "password": "..." }

        Credentials go to the OS keychain (local) — they never leave the user's
        machine in a developer-readable form (Keychain is owned by the OS).
        Cloud Run deployments fall back to Firestore with Fernet encryption.
        """
        account = get_account(account_id)
        if account is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Account not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "username and password are required"})
            return

        bank_key = account.get("bank_key")
        try:
            import sys as _sys
            if str(ROOT) not in _sys.path:
                _sys.path.insert(0, str(ROOT))
            from bank_fetcher import _load_fetcher_config  # noqa: PLC0415
            from bank_fetcher.credential_manager import store_credentials_cloud  # noqa: PLC0415
            import keyring  # noqa: PLC0415

            cfg = _load_fetcher_config(bank_key)
            saved_to = None
            # Prefer the OS keychain — it's tied to the user's logged-in macOS
            # session and isn't accessible to other apps without the user's
            # consent. The backend code can READ it (via the keyring library)
            # but only while running under the same user account.
            svc = cfg.get("keyring_service")
            u_key = cfg.get("keyring_username_key")
            p_key = cfg.get("keyring_password_key")
            uid_prefix = f"{get_current_uid()}:" if get_current_uid() else ""
            if svc and u_key and p_key:
                try:
                    keyring.set_password(svc, f"{uid_prefix}{u_key}", username)
                    keyring.set_password(svc, f"{uid_prefix}{p_key}", password)
                    saved_to = "keychain"
                except Exception as kc_err:
                    print(f"keychain save failed ({kc_err}); falling back to encrypted Firestore", flush=True)
            if saved_to is None:
                store_credentials_cloud(bank_key, username, password)
                saved_to = "firestore"

            set_needs_login(account_id, False)
            set_has_credentials(account_id, True)
            self._send_json(HTTPStatus.OK, {"saved_to": saved_to})
        except Exception as exc:
            print(f"[save-credentials] error: {exc}", flush=True)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_recategorize_account(self, account_id: int) -> None:
        """POST /api/accounts/<id>/recategorize — re-run categorizer on all stored transactions."""
        from db import get_transactions_for_account_raw, update_transaction_by_id

        account = get_account(account_id)
        if account is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Account not found"})
            return

        try:
            txns = get_transactions_for_account_raw(account_id)

            if not txns:
                self._send_json(HTTPStatus.OK, {"updated": 0})
                return

            # Repair amount_type when the extension stored everything as the
            # same type. The stored `amount` field has the sign stripped, so
            # we use a description-based heuristic instead: most rows are
            # expenses; only flip to credit when the description contains a
            # strong "money in" signal (payroll, deposit, refund, etc.) or
            # the category is Income/Refund.
            acct_type = (account.get("account_type") or "").lower()
            for t in txns:
                t["amount_type"] = _infer_amount_type(
                    description=str(t.get("description") or ""),
                    merchant_clean=str(t.get("merchant_clean") or ""),
                    category=str(t.get("category") or ""),
                    account_type=acct_type,
                )
                t["type"] = t["amount_type"]

            _categorize_scraped(txns, account_type=acct_type or None)

            for t in txns:
                update_transaction_by_id(
                    t["id"],
                    t.get("category"),
                    t.get("merchant_clean") or t.get("description"),
                    amount_type=t.get("amount_type"),
                )

            self._send_json(HTTPStatus.OK, {"updated": len(txns)})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_import_transactions(self) -> None:
        """POST /api/import — receive transactions scraped by a bookmarklet and store in DB."""
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        bank_key = body.get("bank_key", "unknown")
        transactions = body.get("transactions", [])
        account_id = body.get("account_id")

        if not transactions:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "No transactions provided"})
            return

        try:
            import sys as _sys
            _proj_root = str(ROOT)
            if _proj_root not in _sys.path:
                _sys.path.insert(0, _proj_root)
            from bank_fetcher import _load_fetcher_config  # noqa: PLC0415
            cfg = _load_fetcher_config(bank_key)
            display_name = cfg.get("bank_display_name", bank_key)
        except Exception:
            display_name = bank_key.replace("_", " ").title()

        # Find or create account for this bank
        if account_id is None:
            accounts = get_accounts()
            match = next((a for a in accounts if a["bank_key"] == bank_key), None)
            if match:
                account_id = match["id"]
            else:
                acct_type_bm = cfg.get("account_type", "checking") if cfg else "checking"
            account_id = add_account(bank_key, display_name, sync_period="all", account_type=acct_type_bm)

        _categorize_scraped(transactions)
        store_transactions(account_id, transactions)
        update_last_synced(account_id, datetime.now())
        set_needs_login(account_id, False)
        account = get_account(account_id)
        print(f"BOOKMARKLET IMPORT: {display_name} — {len(transactions)} transactions")
        self._send_json(HTTPStatus.OK, {
            "account": account,
            "count": len(transactions),
        })

    def _handle_delete_account(self, account_id: int) -> None:
        """DELETE /api/accounts/<id> — remove account and all its transactions."""
        if get_account(account_id) is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Account not found"})
            return
        delete_account(account_id)
        self._send_json(HTTPStatus.OK, {"deleted": account_id})

    def _handle_patch_account(self, account_id: int) -> None:
        """PATCH /api/accounts/<id> — update auto-sync settings and/or current balance."""
        if get_account(account_id) is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Account not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        set_auto_sync(
            account_id,
            enabled=body.get("auto_sync"),
            interval_hours=body.get("sync_interval_hours"),
        )
        if "account_balance" in body:
            set_account_balance(account_id, str(body["account_balance"]).strip())
        if "needs_login" in body:
            set_needs_login(account_id, bool(body["needs_login"]))
        if "status" in body:
            error_msg = body.get("last_error") or (
                "Session expired — please re-login." if body["status"] == "needs_relogin" else None
            )
            set_account_status(account_id, body["status"], error_msg)
        self._send_json(HTTPStatus.OK, get_account(account_id))

    def _resolve_persist_mode(self) -> str:
        configured = os.environ.get("PERSIST_MODE", "").strip().lower()
        if configured:
            return configured
        # Auto mode:
        # - Cloud deployments default to Firebase Storage
        # - Local runs default to local filesystem persistence
        if os.environ.get("K_SERVICE") or os.environ.get("GAE_ENV"):
            return "firebase_storage"
        return "local"

    def _log_category_summary(self, transactions: list[dict]) -> None:
        counts: dict[str, int] = {}
        for txn in transactions:
            category = txn.get("category") or "Uncategorized"
            counts[category] = counts.get(category, 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"DEBUG: category counts: {summary}")


def _run_due_syncs() -> None:
    """Check all accounts and headlessly sync any that are overdue."""
    import sys as _sys
    if str(ROOT) not in _sys.path:
        _sys.path.insert(0, str(ROOT))
    from bank_fetcher import _load_fetcher_config, scrape_recent_transactions_headless  # noqa: PLC0415
    from bank_fetcher.base_crawler import SessionExpiredError  # noqa: PLC0415
    from bank_fetcher.credential_manager import get_credentials  # noqa: PLC0415

    for uid in list_user_ids():
        uid_token = set_current_uid(uid)
        try:
            for acct in get_accounts():
                if not acct.get("auto_sync"):
                    continue
                last_sync = acct.get("last_synced_at")
                if last_sync:
                    interval_hours = acct.get("sync_interval_hours") or 6
                    hours_since = (datetime.now() - datetime.fromisoformat(last_sync)).total_seconds() / 3600
                    if hours_since < interval_hours:
                        continue
                session_path = ROOT / "data" / "sessions" / f"{uid}_{acct['id']}.json"
                last_url = acct.get("last_url")
                # Use latest stored transaction date (not last_synced_at) so we stay minimal
                # even if auto-sync was failing for a while due to session expiry.
                since_date = _smart_since_date(acct["id"])
                bank_key = acct["bank_key"]
                print(f"AUTO-SYNC: syncing {acct['display_name']}…")

                try:
                    # ── Step 1: try fast headless sync with saved session cookies ──
                    if session_path.exists() and last_url:
                        txns = scrape_recent_transactions_headless(
                            bank_key=bank_key,
                            since_date=since_date,
                            session_path=session_path,
                            account_url=last_url,
                        )
                    else:
                        raise SessionExpiredError("No saved session — falling back to credential login")

                    _categorize_scraped(txns)
                    store_transactions(acct["id"], txns)
                    update_last_synced(acct["id"], datetime.now())
                    set_needs_login(acct["id"], False)
                    print(f"AUTO-SYNC: {acct['display_name']} — {len(txns)} new transactions")

                except SessionExpiredError:
                    # ── Step 2: session expired — re-login using saved keychain credentials ──
                    print(f"AUTO-SYNC: {acct['display_name']} — session expired, re-logging in with keychain…")
                    try:
                        fetcher_cfg = _load_fetcher_config(bank_key)
                        creds = get_credentials(fetcher_cfg)

                        from bank_fetcher.base_crawler import BankCrawler  # noqa: PLC0415
                        from bank_fetcher import DEFAULT_RAW_DIR  # noqa: PLC0415
                        crawler = BankCrawler(fetcher_cfg=fetcher_cfg, raw_dir=DEFAULT_RAW_DIR)
                        records, new_last_url = crawler.run_scrape_automated(
                            username=creds.username,
                            password=creds.password,
                            since_date=since_date,
                            session_save_path=session_path,   # save fresh session cookies
                        )
                        txns = [
                            {"date": r.date, "description": r.description,
                             "amount": r.amount, "type": r.amount_type}
                            for r in records
                        ]
                        _categorize_scraped(txns)
                        store_transactions(acct["id"], txns)
                        update_last_synced(acct["id"], datetime.now())
                        set_needs_login(acct["id"], False)
                        if new_last_url:
                            set_last_url(acct["id"], new_last_url)
                        print(f"AUTO-SYNC: {acct['display_name']} — re-login OK, {len(txns)} new transactions")

                    except Exception as relogin_exc:
                        # Credentials missing or MFA required — user must log in manually
                        set_needs_login(acct["id"], True)
                        print(f"AUTO-SYNC: {acct['display_name']} — re-login failed ({relogin_exc}), marking needs_login")

                except Exception as exc:
                    print(f"AUTO-SYNC: {acct['display_name']} — error: {exc}")
        finally:
            reset_current_uid(uid_token)


# Circuit breaker state for the auto-sync scheduler. When Firestore returns
# 429 (quota exceeded), we pause for FIFO_QUOTA_PAUSE_SEC instead of letting
# firebase-admin's internal retry logic burn through more of the recovering
# quota. Module-level vars so the breaker survives across loop iterations.
_FIFO_QUOTA_TRIPPED_UNTIL = 0.0
FIFO_QUOTA_PAUSE_SEC = 3600   # 1 hour after a 429
FIFO_SCHEDULER_INTERVAL_SEC = 300  # 5 minutes (was 60s; reduces idle reads 5x)


def _is_quota_error(exc: BaseException) -> bool:
    """Detect Firestore 429 / quota-exceeded errors, including the ones
    firebase-admin wraps as DeadlineExceeded after internal retries.
    """
    msg = str(exc).lower()
    return ("429" in msg) or ("quota exceeded" in msg) or ("resource exhausted" in msg)


def _start_auto_sync_scheduler() -> None:
    """Daemon thread that checks for overdue auto-syncs every 5 minutes.

    Includes a circuit breaker: if Firestore reports quota-exceeded, the
    scheduler pauses for 1 hour instead of retrying every cycle. That stops
    the retry storm from burning even more of the recovering daily quota.
    """
    global _FIFO_QUOTA_TRIPPED_UNTIL

    def loop() -> None:
        global _FIFO_QUOTA_TRIPPED_UNTIL
        while True:
            time.sleep(FIFO_SCHEDULER_INTERVAL_SEC)
            now = time.time()
            if now < _FIFO_QUOTA_TRIPPED_UNTIL:
                remaining = int(_FIFO_QUOTA_TRIPPED_UNTIL - now)
                print(f"AUTO-SYNC paused: quota cooldown ({remaining}s remaining)")
                continue
            try:
                _run_due_syncs()
            except Exception as exc:
                if _is_quota_error(exc):
                    _FIFO_QUOTA_TRIPPED_UNTIL = time.time() + FIFO_QUOTA_PAUSE_SEC
                    print(
                        f"AUTO-SYNC quota-exceeded — pausing scheduler for "
                        f"{FIFO_QUOTA_PAUSE_SEC // 60} minutes to let quota recover"
                    )
                else:
                    print(f"AUTO-SYNC scheduler error: {exc}")
    threading.Thread(target=loop, daemon=True, name="auto-sync-scheduler").start()
    print(f"AUTO-SYNC scheduler started (every {FIFO_SCHEDULER_INTERVAL_SEC}s, quota-aware)")


def _backfill_missing_categories() -> None:
    """
    On startup, find any stored transactions with NULL category or merchant_clean
    and run them through the parser + categorizer.  Runs in a background thread
    so it doesn't delay server startup.

    GATED: scans up to 5000 transactions × Firestore reads per startup. Set
    FIFO_BACKFILL=1 to enable. Defaults off to protect the daily quota.
    """
    if os.environ.get("FIFO_BACKFILL", "").strip() != "1":
        print("BACKFILL: skipped on startup (set FIFO_BACKFILL=1 to enable)", flush=True)
        return
    from db import get_uncategorized_transactions, update_transaction_by_id

    def _run() -> None:
        try:
            for uid in list_user_ids():
                uid_token = set_current_uid(uid)
                try:
                    txns = get_uncategorized_transactions(limit=5000)
                    if not txns:
                        continue
                    print(f"BACKFILL: re-categorizing {len(txns)} transactions with missing categories for {uid}…")
                    # Map DB fields to the format _categorize_scraped expects
                    for t in txns:
                        t["type"] = t.get("amount_type", "debit")
                    _categorize_scraped(txns)
                    for t in txns:
                        update_transaction_by_id(
                            t["id"],
                            t.get("category"),
                            t.get("merchant_clean") or t.get("description"),
                        )
                    print(f"BACKFILL: done — {len(txns)} transactions updated for {uid}")
                finally:
                    reset_current_uid(uid_token)
        except Exception as exc:
            print(f"BACKFILL error: {exc}")

    threading.Thread(target=_run, daemon=True, name="category-backfill").start()


def main() -> int:
    print("FIFO server v2 — smart-connect=YES pythonpath=YES", flush=True)
    init_db()
    _backfill_missing_categories()
    _start_auto_sync_scheduler()
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "8000"))
    server = None
    for attempt in range(10):
        try:
            server = ThreadingHTTPServer((host, port), DemoHandler)
            break
        except OSError as exc:
            if exc.errno != 48:
                raise
            port += 1
    if server is None:
        raise OSError("No available port found starting at 8000")
    url = f"http://127.0.0.1:{port}/about.html"
    print(f"Serving Finely demo on {url}")
    if not os.environ.get("NO_BROWSER"):
        try:
            webbrowser.open(url, new=2)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
