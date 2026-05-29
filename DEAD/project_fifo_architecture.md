# FIFO — Production Architecture

> Last updated: April 2026

---

## Overview

FIFO is a personal finance app that automatically imports, categorizes, and visualizes
bank transactions. It consists of a static frontend, a Python backend on Cloud Run, and
a Chrome extension that handles bank scraping without bot detection.

---

## System Diagram

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │  User's Browser                                                      │
 │                                                                      │
 │  ┌─────────────────────┐        ┌───────────────────────────────┐   │
 │  │  FIFO Web App        │        │  FIFO Chrome Extension         │   │
 │  │  (Cloudflare Pages)  │◄──────►│  (chrome_extension/)          │   │
 │  │  website/demo.html   │ post   │  • Opens bank tab              │   │
 │  │  website/styles.css  │ Msg    │  • User logs in normally        │   │
 │  └────────┬────────────┘        │  • Scrapes DOM transactions     │   │
 │           │ fetch               │  • POSTs to Cloud Run backend   │   │
 │           ▼                     └──────────────────┬──────────────┘   │
 │  ┌─────────────────────┐                           │                  │
 │  │  Bank Website        │                           │                  │
 │  │  (BofA, etc.)        │◄──────────────────────────┘                  │
 │  │  Real session, no    │  User logs in, extension reads DOM           │
 │  │  bot detection       │                                              │
 │  └─────────────────────┘                                              │
 └───────────────────────────────────────────┬─────────────────────────┘
                                             │ HTTPS
                                             ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Cloud Run  (us-central1)                                             │
 │  Service: finance-demo                                                │
 │  URL: finance-demo-486722019708.us-central1.run.app                   │
 │                                                                       │
 │  Container: python:3.11-slim + Playwright/Chromium                    │
 │  Entry: src/serve_demo.py  (stdlib HTTPServer, port 8000)             │
 │  Memory: 2 GiB   CPU: 2   Min-instances: 1   Max: 3                  │
 │                                                                       │
 │  Key endpoints:                                                       │
 │    GET  /api/accounts                  — list connected accounts      │
 │    POST /api/accounts                  — add account                  │
 │    GET  /api/accounts/<id>/transactions                               │
 │    GET  /api/accounts/<id>/insights                                   │
 │    POST /api/smart-connect/extension-ingest  ← Extension data entry  │
 │    POST /api/import                    — manual PDF/CSV upload        │
 │    POST /api/parse                     — parse PDF statement          │
 │    GET  /api/version                   — health + version check       │
 └───────────────────────────────────┬──────────────────────────────────┘
                                     │
                                     ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Firebase Storage  (financesoftware-5c8e4.firebasestorage.app)        │
 │                                                                       │
 │  Stores per-user:                                                     │
 │    <username>/raw/<statement>.pdf                                     │
 │    <username>/processed/acc_<last4>.csv   (append + dedupe)           │
 │                                                                       │
 │  SQLite DB (in-container, ephemeral between deploys):                 │
 │    accounts, transactions, categories                                 │
 └──────────────────────────────────────────────────────────────────────┘
```

---

## Component Details

### 1. FIFO Web App — `website/`

Hosted on **Cloudflare Pages** as a static site.

- `demo.html` — single-page app; all UI logic in-page JS
- `styles.css` — all styling
- `about.html` — landing/marketing page

The page communicates with Cloud Run via `fetch()` using `CLOUD_API_BASE`.
When the FIFO Chrome Extension is installed, Smart Connect routes through it
via `window.postMessage` instead of calling the backend directly.

### 2. Cloud Run Backend — `src/serve_demo.py`

Python stdlib `HTTPServer` running on port 8000. No framework.

Responsibilities:
- Serve `demo.html` and static assets
- REST API for accounts, transactions, insights
- Categorize transactions using the DistilBERT model (`categorizer/`)
- Store/retrieve data from Firebase Storage and the in-container SQLite DB
- Accept transaction data from the Chrome Extension via `/api/smart-connect/extension-ingest`

**Important:** `PYTHONPATH=/app` is set in the Dockerfile so that
`bank_fetcher`, `core`, `categorizer` etc. are importable by `src/serve_demo.py`.

### 3. FIFO Chrome Extension — `chrome_extension/`

The long-term solution for bank scraping. Runs inside the user's own Chrome
browser, so there is zero bot detection risk.

**Files:**
```
chrome_extension/
  manifest.json               — MV3 manifest
  background.js               — Service worker: orchestrates the sync flow
  content_scripts/
    fifo_bridge.js            — Injected into FIFO pages; relays postMessage ↔ chrome.runtime
  popup/
    popup.html / popup.js     — Extension popup for standalone sync trigger
  icons/
    icon16.png, icon48.png, icon128.png
```

**Sync flow:**
1. User clicks "⚡ Let FIFO handle it" in the FIFO web app
2. FIFO page fires `window.postMessage({type:'FIFO_SMART_CONNECT', bank:'boa'})`
3. `fifo_bridge.js` relays this to `background.js` via `chrome.runtime.sendMessage`
4. `background.js` opens `https://www.bankofamerica.com/` in a new tab
5. User logs in normally (real browser, real IP — no bot detection)
6. `chrome.tabs.onUpdated` detects when user reaches `/myaccounts`
7. Background navigates to the transactions/activity page
8. `chrome.scripting.executeScript` injects `scrapeBoaTransactions()` to read DOM
9. Background POSTs `{bank_key, transactions}` to `/api/smart-connect/extension-ingest`
10. Backend categorizes + stores transactions; returns success
11. Background notifies FIFO tab; page shows ✅ and reloads account data

**Installing the extension (developer mode):**
1. Open `chrome://extensions`
2. Enable "Developer mode" (top right)
3. Click "Load unpacked" → select the `chrome_extension/` folder
4. The ⚡ icon appears in the Chrome toolbar

### 4. Bank Fetcher — `bank_fetcher/`

Playwright-based headless scraper (currently unused for Smart Connect — replaced by
the Chrome Extension). Still used for local testing and future background sync features.

Config files live in `bank_fetcher/configs/`:
- `boa_fetcher_config.json` — BofA selectors, login steps, timeouts

### 5. Categorizer — `categorizer/`

DistilBERT model fine-tuned on 50k transactions. Classifies transactions into
standard personal finance categories (Food, Transport, Housing, etc.).

Model path: `categorizer/models/bert_distilbert_50k_full`
Entry: `categorize.py` → `categorize_transactions(list[dict])`

### 6. CI/CD — `.github/workflows/deploy-cloud-run.yml`

GitHub Actions workflow triggers on:
- Push to `main` that touches `src/**`, `bank_fetcher/**`, `Dockerfile`, `requirements.txt`, or the workflow file itself
- Manual `workflow_dispatch`

Steps:
1. Authenticate to GCP using `GCP_SA_KEY` (Workload Identity / service account JSON)
2. `gcloud auth configure-docker us-central1-docker.pkg.dev`
3. `docker build --no-cache` → push to Artifact Registry
4. `gcloud run deploy finance-demo`

**Secrets required in GitHub repo Settings → Secrets:**
- `GCP_SA_KEY` — service account JSON (firebase-adminsdk or dedicated deploy SA)
- `GCP_PROJECT_ID` — `financesoftware-5c8e4`

**GCP resources:**
- Project: `financesoftware-5c8e4`
- Artifact Registry repo: `us-central1-docker.pkg.dev/financesoftware-5c8e4/finance-demo/finance-demo`
- Cloud Run service: `finance-demo` (region: `us-central1`)

---

## Key URLs

| Resource | URL |
|---|---|
| FIFO Web App | `https://fifo-app.pages.dev` (Cloudflare Pages) |
| Cloud Run API | `https://finance-demo-486722019708.us-central1.run.app` |
| Firebase Project | `financesoftware-5c8e4` |
| GitHub Repo | `https://github.com/rishuandshreya/FIFO` |

---

## Environment Variables (Cloud Run)

| Variable | Value | Purpose |
|---|---|---|
| `CATEGORIZER_MODEL_DIR` | `/app/categorizer/models/bert_distilbert_50k_full` | ML model path |
| `PLAYWRIGHT_BROWSERS_PATH` | `/ms-playwright` | Chromium binary location |
| `PYTHONPATH` | `/app` | Makes `bank_fetcher`, `core`, etc. importable |
| `PORT` | `8000` | HTTP server port |
| `PERSIST_MODE` | `firebase_storage` (default in prod) | Storage backend |
| `GOOGLE_APPLICATION_CREDENTIALS` | (from Firebase service account) | Firebase auth |
| `FIREBASE_STORAGE_BUCKET` | `financesoftware-5c8e4.firebasestorage.app` | Storage bucket |

---

## What Was Intentionally Removed / Deprecated

| Component | Reason |
|---|---|
| Headless Smart Connect (server-side Playwright login) | BofA bot detection blocks Google Cloud IPs; replaced by Chrome Extension |
| `db_sqlite.py` / `db_firestore.py` split | Consolidated back to single `db.py` |
| Cloud Build (`gcloud builds submit`) | Replaced by `docker build` on GitHub Actions runner (avoids Cloud Build API dependency) |
| Old Cloud Run service URL `558632999562` | Wrong service; correct one is `486722019708` |

---

## Future Roadmap

- **Mobile app:** Use a WebView (WKWebView on iOS, WebView2 on Android) with injected JS for bank scraping — same principle as the Chrome Extension but inside the native app.
- **More banks:** Capital One, Discover, Chase (add `chrome_extension/background.js` bank config + `bank_fetcher/configs/` entry per bank).
- **Plaid fallback:** For banks that don't support DOM scraping, offer Plaid OAuth as an alternative connection method.
- **Background auto-sync:** Scheduled Cloud Run job that calls `/api/accounts/<id>/sync` for accounts that were connected via the Extension (re-opens bank tab, re-scrapes silently).
