# FIFO — Personal Finance, Auto-Synced

FIFO is a personal finance platform that pulls your bank transactions in
automatically, categorizes them, and shows spending, income, cash flow, and
runway across all your accounts — in one place.

It gets your data two ways:

1. **Chrome extension (primary)** — syncs transactions straight from your bank,
   running inside your own logged-in Chrome. No passwords shared with a server,
   and **no OTP** (because it's your real browser, not a headless bot).
2. **PDF statement upload (fallback)** — drop a statement PDF into the web app
   and it parses and imports the transactions.

Once connected, accounts keep themselves fresh (background sync every ~6 hours)
or refresh on demand with **Sync Now**.

---

## How it works

```
  ┌─────────────────────────┐        ┌──────────────────────────────┐
  │  Chrome Extension        │        │  Backend  (Cloud Run / local)│
  │  "thin harvester"        │        │  src/serve_demo.py           │
  │                          │  raw   │                              │
  │  • auto-login (saved     │  rows  │  • normalize raw rows        │
  │    creds, in YOUR Chrome)│──────▶ │    (scrape_normalize.py)     │
  │  • navigate the bank SPA │        │  • categorize (categorize.py)│
  │  • harvest raw cell text │        │  • dedupe + store (db.py →   │
  │  • paginate              │        │    Firestore)                │
  └─────────────────────────┘        │  • detect currency           │
                                      │  • serve the web app         │
  ┌─────────────────────────┐        │                              │
  │  PDF upload  ────────────┼──────▶ │  parse_bank.py (pdfplumber)  │
  └─────────────────────────┘        └───────────────┬──────────────┘
                                                      │
                                          ┌───────────▼──────────────┐
                                          │  Web app (website/)       │
                                          │  Dashboard · Transactions │
                                          │  · Insights (vanilla JS)  │
                                          └──────────────────────────┘
```

**Design principle:** the extension contains **no business logic** — it only does
what must run inside the bank's page (log in, click through, read raw DOM text)
and posts the raw rows to the server. **All parsing/normalization lives on the
server** (`src/scrape_normalize.py`): date parsing, credit/debit direction,
amount parsing, currency detection, dedup, categorization. This means logic
changes ship by deploying the server — the extension rarely needs a Web Store
republish.

Supported banks (scrape config lives server-side in `serve_demo.py`):
**Bank of America, ICICI, Capital One, Discover.** Adding a bank that uses the
existing navigation actions is a server-config change — no extension update.

---

## Repository layout

```
src/
  serve_demo.py        ThreadingHTTPServer: web app + JSON API + ingest endpoints
  scrape_normalize.py  Server-side parsing of raw scraped rows (dates, direction,
                       amounts, year-fix) — the "brain" the extension used to hold
  parse_bank.py        PDF statement parser (pdfplumber), config-driven per bank
  categorize.py        Rule-based transaction categorization (config/category_rules.json)
  db.py                Firestore access: accounts + transactions, dedup, currency
  auth.py              User-scoped auth (accounts isolated per user)
  cloud_persistence.py Firebase Storage for raw PDFs
  new_bank_setup.py    Auto-generates a parser config for an unknown bank

chrome_extension/      MV3 extension: content.js (thin harvester), background.js
                       (sync orchestration + posts raw rows), bridge.js, popup

website/               Web app frontend (demo.html + about.html), vanilla JS/CSS

config/
  banks/*.json         Per-bank PDF-parser configs (markers, patterns)
  category_rules.json  Categorization rules
  common_config.json   Shared aliases / token replacements

bank_fetcher/          Playwright configs for interactive first-connect (login_steps)
tests/                 test_scrape_normalize.py — parser drift-guard vs real configs
Dockerfile, deploy.sh  Container + manual deploy
.github/workflows/     deploy-cloud-run.yml — CI/CD to Cloud Run
```

---

## Run it locally

**Requirements:** Python 3.11, and Google Application Default Credentials for
Firestore (`gcloud auth application-default login`, or a service-account JSON via
`GOOGLE_APPLICATION_CREDENTIALS`).

```bash
pip install -r requirements.txt
python3 src/serve_demo.py            # serves on http://127.0.0.1:8000
```

Open **http://127.0.0.1:8000/about.html** (or `/demo.html` for the app directly).

To exercise the **extension** locally:

1. `chrome://extensions` → enable Developer mode → **Load unpacked** →
   select `chrome_extension/`.
2. Open the local app, **Connect account**, and Sync.

> The extension's content script only runs on `localhost`, `127.0.0.1`, and the
> deployed Cloud Run URL — so use one of those origins, not a static file server.

### Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `PORT` | Server port | `8000` |
| `PERSIST_MODE` | `local` (files under `my_finance/`) or `firebase_storage` | `local` |
| `FIRESTORE_DATABASE` | Named Firestore DB (cloud uses `finance-database`) | `(default)` |
| `FIREBASE_STORAGE_BUCKET` | Bucket for raw PDFs in firebase mode | — |
| `GOOGLE_APPLICATION_CREDENTIALS` | Service-account JSON path (local Firestore) | ADC |
| `NO_BROWSER` | Set to skip auto-opening a browser tab on start | — |

> **Note:** with no `FIRESTORE_DATABASE`, local dev talks to the `(default)`
> Firestore database, which is **separate** from the cloud's `finance-database`.
> To share data with the cloud, run with `FIRESTORE_DATABASE=finance-database`.

---

## Deployment

The backend runs on **Google Cloud Run**, and deploys **automatically** via
GitHub Actions.

- Pushing to `main` triggers `.github/workflows/deploy-cloud-run.yml` **when the
  change touches** `src/`, `bank_fetcher/`, `core/`, `api/`, `website/`,
  `requirements.txt`, `Dockerfile`, or the workflow itself. It builds the
  container and deploys to the `finance-demo` service.
- So **backend and web-app changes ship on push — no manual step.**
- Manual deploy (if ever needed): `bash deploy.sh` (requires `gcloud` + Docker),
  or `gcloud builds submit` + `gcloud run deploy` without local Docker.

> Editing files under `.github/workflows/` and pushing requires a token with the
> `workflow` scope: `gh auth refresh -h github.com -s workflow`.

### The Chrome extension is separate

The extension is **not** part of CI/CD — it's distributed through the Chrome Web
Store and updated manually:

1. Build the upload zip from `chrome_extension/` (strip `localhost`/`127.0.0.1`
   from the manifest — the store rejects them).
2. Upload to the Web Store dashboard and submit for review.

Thanks to the thin-harvester design, this is rare — most changes are server-side.

---

## Data & correctness notes

- **Dedup** — each transaction's identity is a hash of
  `account + date + normalized-description + signed-amount` (the description
  carries the bank's own unique reference, which distinguishes real same-day
  repeats). A running-balance is intentionally **not** in the key (it's read
  inconsistently and would forge duplicates). An occurrence-index tiebreaker
  keeps genuinely-identical, reference-less repeats distinct.
- **Currency** — detected from the actual scraped amounts (₹ / $ / € / £ / ¥)
  and stored per account, so the UI shows the right currency for any bank/country
  without hardcoding.
- **Dates** — banks that render a date with no year (e.g. ICICI's "29 Jun") are
  corrected sequence-aware (newest-first, drop a year at each month wrap).

---

## Tests

```bash
python3 tests/test_scrape_normalize.py      # or: pytest tests/
```

Covers the server-side normalizers (amount/date/direction/year-fix) and includes
a drift-guard that runs against the **real** `serve_demo.py` scrape configs.

---

## Tech stack

- **Backend:** Python 3.11, `http.server`, pdfplumber, firebase-admin /
  google-cloud-firestore, Playwright (interactive first-connect), keyring.
- **Frontend:** vanilla JS / HTML / CSS (no framework), Chart.js.
- **Extension:** Chrome Manifest V3.
- **Infra:** Docker, Google Cloud Run, Firestore, Firebase Storage, GitHub Actions.
