"""
api/main.py — FIFO FastAPI application.

Replaces src/serve_demo.py as the primary web server.
The existing serve_demo.py remains untouched as a fallback.

Start the new server from the project root:
    uvicorn api.main:app --reload --port 8000

All existing frontend URLs (/demo.html, /about.html, /api/*) continue to work
because we replicate the same API contract and serve the same website/ directory.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Make src/ and project root importable (parse_bank, categorize, bank_fetcher…)
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from core.db import init_db
from api.routers import accounts, transactions, insights, plaid_router

WEB_ROOT = ROOT / "website"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────
    init_db()
    # Warm the encryption key cache while the main thread has Keychain access.
    # Background scheduler threads get -25320 (errSecInteractionNotAllowed) if
    # they try to read the Keychain directly; caching here avoids that.
    try:
        from core.encryption import _get_or_create_key
        _get_or_create_key()
        print("[startup] Encryption key loaded into memory cache.")
    except Exception as exc:
        print(f"WARNING: Could not pre-load encryption key: {exc}")
    try:
        from worker.scheduler import start_scheduler
        start_scheduler()
        print("Background refresh scheduler started.")
    except Exception as exc:
        print(f"WARNING: Scheduler could not start: {exc}")
    yield
    # ── Shutdown ──────────────────────────────────────────────────────
    try:
        from worker.scheduler import stop_scheduler
        stop_scheduler()
    except Exception:
        pass


app = FastAPI(
    title="FIFO",
    description="Personal finance aggregator",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API routers — must be registered BEFORE the static mount ─────────
app.include_router(accounts.router,     prefix="/api")
app.include_router(transactions.router, prefix="/api")
app.include_router(insights.router,     prefix="/api")
app.include_router(plaid_router.router, prefix="/api")


# /api/parse — PDF upload (kept at top-level for backward compat)
from fastapi import UploadFile, File, Form
from typing import Optional
import tempfile, os

@app.post("/api/parse")
async def parse_pdf(
    file: UploadFile = File(...),
    username: Optional[str] = Form(None),
):
    """Parse a PDF bank statement and return structured JSON."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Please upload a PDF file")

    pdf_bytes = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)

    try:
        from parse_bank import parse_pdf_to_payload, persist_uploaded_statement
        from categorize import categorize_transactions
        from new_bank_setup import bootstrap_config_if_unknown

        auto_bootstrap = (
            os.environ.get("AUTO_BOOTSTRAP_CONFIG", "1").strip().lower()
            not in {"0", "false", "no"}
        )
        bootstrap_result = None
        if auto_bootstrap:
            bootstrap_result = bootstrap_config_if_unknown(
                tmp_path, config_dir=ROOT / "config" / "banks"
            )

        payload = parse_pdf_to_payload(tmp_path)
        payload["transactions"] = categorize_transactions(payload.get("transactions", []))
        if bootstrap_result:
            payload["config_bootstrap"] = bootstrap_result

        try:
            storage = persist_uploaded_statement(
                payload=payload,
                original_filename=file.filename,
                pdf_bytes=pdf_bytes,
                username=username or "default",
            )
            payload["storage"] = storage
        except Exception as exc:
            payload["storage"] = {"error": str(exc)}

        return payload
    except Exception as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        tmp_path.unlink(missing_ok=True)


@app.middleware("http")
async def no_cache_html(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.endswith(".html") or path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def root():
    return RedirectResponse(url="/about.html")


# ── Static file serving (website/) — MUST be last ────────────────────
# The StaticFiles mount acts as a catch-all fallback; all /api/* routes
# above take precedence because they're registered first.
app.mount("/", StaticFiles(directory=str(WEB_ROOT)), name="static")
