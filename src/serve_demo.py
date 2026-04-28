#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import queue as _queue
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

from categorize import categorize_transactions
from parse_bank import parse_pdf_to_payload, persist_uploaded_statement, merchant_clean as _merchant_clean_fn
from new_bank_setup import bootstrap_config_if_unknown
from db import (
    init_db, add_account, get_accounts, get_account,
    delete_account, store_transactions, get_transactions, update_last_synced,
    set_auto_sync, set_needs_login, set_last_url, set_account_balance,
    set_plaid_item_id,
)


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
WEB_ROOT = ROOT / "website"


def _categorize_scraped(transactions: list[dict]) -> list[dict]:
    """
    Run the full parse_bank merchant-cleaning pipeline on each scraped transaction,
    then categorize using the ML model.
    Mutates each dict in-place, adding 'merchant_clean' and 'category'.
    """
    if not transactions:
        return transactions

    # Step 1: clean every merchant name through the same pipeline used for PDF parsing
    for txn in transactions:
        raw = txn.get("description", "")
        try:
            txn["merchant_clean"] = _merchant_clean_fn(raw)
        except Exception:
            txn["merchant_clean"] = raw

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

    return transactions


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
        account_id = add_account(bank_key, display_name, sync_period="1y")
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
        _categorize_scraped(transactions)
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


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def _add_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

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

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
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

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
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

    def do_OPTIONS(self) -> None:
        # Preflight handler for cross-origin requests.
        self.send_response(HTTPStatus.NO_CONTENT)
        self._add_cors_headers()
        self.end_headers()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/smart-connect/start":
            self._handle_smart_connect_start()
            return
        if path == "/api/smart-connect/mfa":
            self._handle_smart_connect_mfa()
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
        # Plaid endpoints
        if path == "/api/plaid/create-link-token":
            self._handle_plaid_create_link_token()
            return
        if path == "/api/plaid/exchange-token":
            self._handle_plaid_exchange_token()
            return
        if path == "/api/plaid/sandbox-connect":
            self._handle_plaid_sandbox_connect()
            return
        if path.startswith("/api/plaid/sync/"):
            try:
                acct_id = int(path.split("/")[-1])
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid account id"})
                return
            self._handle_plaid_sync(acct_id)
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
        # Parse a PDF statement and return structured JSON.
        if path != "/api/parse":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
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
            args=(session_id, bank_key, username, password),
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
        """GET /api/accounts — list all connected accounts."""
        self._send_json(HTTPStatus.OK, get_accounts())

    def _handle_get_transactions(self, account_id: int) -> None:
        """GET /api/accounts/<id>/transactions?period= — return stored transactions."""
        if get_account(account_id) is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Account not found"})
            return
        qs = parse_qs(urlparse(self.path).query)
        period = qs.get("period", ["all"])[0]
        since_date = _period_to_since_date(period)
        self._send_json(HTTPStatus.OK, get_transactions(account_id, since_date=since_date))

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
            raw_amt = (t.get("amount") or "").replace("$", "").replace(",", "").strip()
            try:
                amt = float(raw_amt)
            except ValueError:
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
            raw_amt = (t.get("amount") or "").replace("$", "").replace(",", "").strip()
            try:
                amt = float(raw_amt)
            except ValueError:
                continue
            cat = t.get("category") or "Uncategorized"
            if cat in _TRANSFER_CATS:
                continue
            atype = t.get("amount_type") or t.get("type") or "debit"
            if atype != "credit":
                name = t.get("merchant_clean") or t.get("description") or "Unknown"
                merchant_totals[name] = round(merchant_totals.get(name, 0.0) + abs(amt), 2)
        top_merchants = sorted(
            [{"name": k, "total": v} for k, v in merchant_totals.items()],
            key=lambda x: x["total"], reverse=True
        )[:10]

        account = get_account(account_id)
        current_balance = account.get("account_balance", "") if account else ""

        self._send_json(HTTPStatus.OK, {
            "total_income": round(total_income, 2),
            "total_expenses": round(total_expenses, 2),
            "net": round(total_income - total_expenses, 2),
            "current_balance": current_balance,
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
            raw_amt = (t.get("amount") or "").replace("$", "").replace(",", "").strip()
            try:
                amt = float(raw_amt)
            except ValueError:
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

    # ── Plaid endpoints ───────────────────────────────────────────────

    def _handle_plaid_sandbox_connect(self) -> None:
        """POST /api/plaid/sandbox-connect — create sandbox item without Plaid Link (bypasses CDN)."""
        try:
            from plaid_connector import create_sandbox_item, save_access_token, fetch_transactions, fetch_balance
            access_token, item_id = create_sandbox_item()
            display_name = "Chase (Sandbox)"
            account_id = add_account("plaid", display_name, sync_period="1y")
            save_access_token(account_id, access_token)
            set_plaid_item_id(account_id, item_id)
            since_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=365)
            transactions = fetch_transactions(account_id, since_date)
            store_transactions(account_id, transactions)
            update_last_synced(account_id, datetime.now())
            bal = fetch_balance(account_id)
            if bal:
                set_account_balance(account_id, bal)
            account = get_account(account_id)
            self._send_json(HTTPStatus.OK, {
                "account": account,
                "count": len(transactions),
                "transactions": transactions,
            })
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_plaid_create_link_token(self) -> None:
        """POST /api/plaid/create-link-token — generate a Plaid Link token for the frontend."""
        try:
            from plaid_connector import create_link_token
            token = create_link_token()
            self._send_json(HTTPStatus.OK, {"link_token": token})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_plaid_exchange_token(self) -> None:
        """POST /api/plaid/exchange-token — exchange public_token for access_token, fetch transactions."""
        try:
            from plaid_connector import (
                exchange_public_token, save_access_token,
                fetch_transactions, fetch_balance,
            )
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            public_token = body["public_token"]
            display_name = body.get("institution_name", "Plaid Account")

            access_token, item_id = exchange_public_token(public_token)
            account_id = add_account("plaid", display_name, sync_period="1y")
            save_access_token(account_id, access_token)
            set_plaid_item_id(account_id, item_id)

            since_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=365)
            transactions = fetch_transactions(account_id, since_date)
            store_transactions(account_id, transactions)
            update_last_synced(account_id, datetime.now())

            bal = fetch_balance(account_id)
            if bal:
                set_account_balance(account_id, bal)

            account = get_account(account_id)
            self._send_json(HTTPStatus.OK, {
                "account": account,
                "count": len(transactions),
                "transactions": transactions,
            })
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_plaid_sync(self, account_id: int) -> None:
        """POST /api/plaid/sync/<id> — delta-sync a Plaid-connected account."""
        try:
            from plaid_connector import fetch_transactions, fetch_balance
            account = get_account(account_id)
            if account is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Account not found"})
                return
            last_sync = account.get("last_synced_at")
            since_date = (
                datetime.fromisoformat(last_sync)
                if last_sync
                else datetime.now() - timedelta(days=365)
            )
            transactions = fetch_transactions(account_id, since_date)
            store_transactions(account_id, transactions)
            update_last_synced(account_id, datetime.now())
            bal = fetch_balance(account_id)
            if bal:
                set_account_balance(account_id, bal)
            self._send_json(HTTPStatus.OK, {"count": len(transactions)})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_add_account(self) -> None:
        """POST /api/accounts — connect a new bank account, scrape, store."""
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
            display_name = cfg.get("bank_display_name", bank_key)
            account_id = add_account(bank_key, display_name, sync_period=period)
            session_path = ROOT / "data" / "sessions" / f"{account_id}.json"
            transactions, last_url, ending_balance, starting_balance = scrape_recent_transactions(
                bank_key=bank_key, since_date=since_date, session_save_path=session_path
            )
            _categorize_scraped(transactions)
            store_transactions(account_id, transactions)
            update_last_synced(account_id, datetime.now())
            if last_url:
                set_last_url(account_id, last_url)
            if ending_balance:
                set_account_balance(account_id, ending_balance)
            set_needs_login(account_id, False)
            account = get_account(account_id)
            self._send_json(HTTPStatus.OK, {
                "account": account,
                "period": period,
                "count": len(transactions),
                "transactions": transactions,
                "ending_balance": ending_balance,
                "starting_balance": starting_balance,
            })
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_sync_account(self, account_id: int) -> None:
        """POST /api/accounts/<id>/sync — re-scrape and update stored transactions."""
        import sys as _sys
        _proj_root = str(ROOT)
        if _proj_root not in _sys.path:
            _sys.path.insert(0, _proj_root)
        from bank_fetcher import scrape_recent_transactions

        account = get_account(account_id)
        if account is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Account not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        period = body.get("period", account["sync_period"] or "1m")

        # Delta sync: only fetch transactions since the last sync (or 1 year if never synced).
        last_sync = account.get("last_synced_at")
        if last_sync:
            since_date = datetime.fromisoformat(last_sync)
        else:
            since_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=365)

        try:
            session_path = ROOT / "data" / "sessions" / f"{account_id}.json"
            transactions, last_url, ending_balance, starting_balance = scrape_recent_transactions(
                bank_key=account["bank_key"],
                since_date=since_date,
                session_save_path=session_path,
            )
            _categorize_scraped(transactions)
            store_transactions(account_id, transactions)
            update_last_synced(account_id, datetime.now())
            if last_url:
                set_last_url(account_id, last_url)
            if ending_balance:
                set_account_balance(account_id, ending_balance)
            set_needs_login(account_id, False)
            self._send_json(HTTPStatus.OK, {
                "account": get_account(account_id),
                "period": period,
                "count": len(transactions),
                "transactions": transactions,
                "ending_balance": ending_balance,
                "starting_balance": starting_balance,
            })
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_relogin_account(self, account_id: int) -> None:
        """
        POST /api/accounts/<id>/relogin — interactive re-login for a specific account.
        Opens a browser window for the user to log in + complete OTP, then fetches only
        transactions newer than last_synced_at. Never creates a duplicate account row.
        """
        import sys as _sys
        _proj_root = str(ROOT)
        if _proj_root not in _sys.path:
            _sys.path.insert(0, _proj_root)
        from bank_fetcher import scrape_recent_transactions

        account = get_account(account_id)
        if account is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Account not found"})
            return

        # Only fetch since last sync — no need to re-download what we already have
        last_sync = account.get("last_synced_at")
        since_date = (
            datetime.fromisoformat(last_sync)
            if last_sync
            else datetime.now() - timedelta(days=365)
        )

        try:
            session_path = ROOT / "data" / "sessions" / f"{account_id}.json"
            transactions, last_url, ending_balance, starting_balance = scrape_recent_transactions(
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
            set_needs_login(account_id, False)
            self._send_json(HTTPStatus.OK, {
                "account":   get_account(account_id),
                "count":     new_count,
                "transactions": transactions,
            })
        except Exception as exc:
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

            for t in txns:
                t["type"] = t.get("amount_type", "debit")

            _categorize_scraped(txns)

            for t in txns:
                update_transaction_by_id(
                    t["id"],
                    t.get("category"),
                    t.get("merchant_clean") or t.get("description"),
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
                account_id = add_account(bank_key, display_name, sync_period="all")

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
            balance_str = str(body["account_balance"]).strip()
            set_account_balance(account_id, balance_str)
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
    from bank_fetcher import scrape_recent_transactions_headless  # noqa: PLC0415
    from bank_fetcher.base_crawler import SessionExpiredError  # noqa: PLC0415

    for acct in get_accounts():
        if not acct.get("auto_sync"):
            continue
        last_sync = acct.get("last_synced_at")
        if last_sync:
            interval_hours = acct.get("sync_interval_hours") or 24
            hours_since = (datetime.now() - datetime.fromisoformat(last_sync)).total_seconds() / 3600
            if hours_since < interval_hours:
                continue
        session_path = ROOT / "data" / "sessions" / f"{acct['id']}.json"
        if not session_path.exists():
            continue
        last_url = acct.get("last_url")
        if not last_url:
            continue
        since_date = datetime.fromisoformat(last_sync) if last_sync else None
        print(f"AUTO-SYNC: syncing {acct['display_name']}…")
        try:
            txns = scrape_recent_transactions_headless(
                bank_key=acct["bank_key"],
                since_date=since_date,
                session_path=session_path,
                account_url=last_url,
            )
            _categorize_scraped(txns)
            store_transactions(acct["id"], txns)
            update_last_synced(acct["id"], datetime.now())
            set_needs_login(acct["id"], False)
            print(f"AUTO-SYNC: {acct['display_name']} — {len(txns)} new transactions")
        except SessionExpiredError:
            set_needs_login(acct["id"], True)
            print(f"AUTO-SYNC: {acct['display_name']} — session expired, needs manual login")
        except Exception as exc:
            print(f"AUTO-SYNC: {acct['display_name']} — error: {exc}")


def _start_auto_sync_scheduler() -> None:
    """Start a daemon thread that checks for overdue auto-syncs every minute."""
    def loop() -> None:
        while True:
            time.sleep(60)
            try:
                _run_due_syncs()
            except Exception as exc:
                print(f"AUTO-SYNC scheduler error: {exc}")
    threading.Thread(target=loop, daemon=True, name="auto-sync-scheduler").start()
    print("AUTO-SYNC scheduler started (checks every 60s)")


def _backfill_missing_categories() -> None:
    """
    On startup, find any stored transactions with NULL category or merchant_clean
    and run them through the parser + categorizer.  Runs in a background thread
    so it doesn't delay server startup.
    """
    from db import get_uncategorized_transactions, update_transaction_by_id

    def _run() -> None:
        try:
            txns = get_uncategorized_transactions(limit=5000)
            if not txns:
                return
            print(f"BACKFILL: re-categorizing {len(txns)} transactions with missing categories…")
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
            print(f"BACKFILL: done — {len(txns)} transactions updated")
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
