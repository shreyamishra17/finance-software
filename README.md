# Finance Software

A personal-finance platform that connects your bank accounts, keeps transactions in sync automatically, categorizes them with a hybrid ML pipeline, and turns them into spending, income, and cash-flow insights — across all your accounts in one place.

The interesting engineering here isn't the dashboard. It's the two hard problems underneath it: **getting clean transaction data out of banks that have no public API**, and **labeling messy, abbreviated merchant strings accurately**. The sections below focus on how those are solved.

**Live demo:** [finance-software.pages.dev/about](https://finance-software.pages.dev/about) — hosted on Cloudflare Pages. If your browser shows a security warning, it's a false-positive flag that free `pages.dev` finance apps commonly trigger; the code in this repository is the authoritative reference.

---

## Architecture at a glance

The design splits cleanly into a **dumb client** and a **smart server**. The browser extension does only what *must* happen inside the bank's page; every bit of parsing, categorization, and storage logic lives server-side.

```
        BANK SITE (in the user's own logged-in browser)
                         │  raw DOM cells (no parsing)
                         ▼
┌───────────────────────────────────┐        ┌──────────────────────────────────────┐
│  Chrome Extension — "thin          │  POST  │  Backend  (Python, Cloud Run)         │
│  harvester"                        │  raw   │                                       │
│                                    │  rows  │  scrape_normalize  →  dates, amounts, │
│  • reads a server-supplied config  │───────▶│                       credit/debit,   │
│  • clicks through the bank's pages │        │                       currency detect │
│  • harvests raw cell text          │        │  categorize        →  4-layer hybrid  │
│  • paginates                       │        │  db                →  dedupe + store  │
│  • ZERO parsing / business logic   │        │                       (Firestore)     │
└───────────────────────────────────┘        └──────────────────┬───────────────────┘
                                                                 │
┌───────────────────────────────────┐                           │
│  PDF statement upload (fallback)   │─── parse_bank (pdfplumber)┤
└───────────────────────────────────┘                           │
                                                     ┌───────────▼───────────┐
                                                     │  Web app (vanilla JS)  │
                                                     │  Dashboard · Txns ·    │
                                                     │  Insights · Trends     │
                                                     └────────────────────────┘
```

**Why this split matters.** The per-bank scraping instructions (which CSS selectors to read, which buttons to click, how to paginate, how to tell credit from debit) live in a single server-side config map and are served to the extension at runtime. So **adding a new bank, or fixing one when it changes its HTML, is a server-side config change — no extension rebuild, no Chrome Web Store re-review.** The extension is a generic, rarely-touched shell; the logic iterates on the server where it can ship instantly.

Four institutions are live today — **Bank of America, Capital One, ICICI, and Discover** — each defined purely as config, not code. Because the per-bank logic is pure config, bringing on a new institution such as **Chase** or **American Express** is a server-side config change (selectors, navigation, pagination), not an extension rebuild or a code change.

---

## The categorization engine

Bank transaction strings are hostile to classify: `MOBILE PURCHASE WHOLEFDS LAJ 100 LA JOLLA CA`, `QUALCOMM TECHNOL DES:DIRECT DEP ID:...`, `TST* BLUE BOTTLE`. The same merchant appears a dozen different ways, padded with processor codes and store numbers.

The engine runs **four layers in order**, stopping at the first confident hit. Cheap and precise first; the ML model only handles what the earlier layers can't:

1. **High-confidence pattern rules** — structured patterns like Zelle/Venmo P2P, direct-deposit payroll, and statement-category tails. Deterministic and near-certain.
2. **Fuzzy merchant matching (RapidFuzz)** — matches a cleaned merchant name against a known-company reference list using `WRatio`, accepting only matches above a 90/100 threshold.
3. **Fine-tuned DistilBERT** — a transformer fine-tuned on transaction merchant strings handles everything the rules miss, but its prediction is only accepted above a 0.7 confidence floor.
4. **Recurrence post-pass** — a statistical pass that catches income the other layers miss: it groups credits by merchant, and promotes any group with a regular cadence (weekly/biweekly/monthly, within tolerance) *and* a consistent amount to "Income" — so an unknown employer's deposits get labeled correctly from their *pattern*, not a keyword.

Every layer is **confidence-gated and fails safe**: if the model isn't available, rows become explicitly `Uncategorized` rather than silently mislabeled. Direction (credit vs debit) is sign-guarded so a recurring *debit* (e.g. a monthly mutual-fund mandate) can never be mistaken for income.

**Merchant normalization** feeds all of this: ACH/bank metadata (`DES:`, `INDN:`, `CO ID:`, `ACH HOLD`, trailing store numbers) is stripped first, so every payment variant of the same merchant collapses to one clean name before matching.

---

## Sync reliability (the part that's easy to underestimate)

Scraping a logged-in bank session in the background is full of edge cases, and most of the backend's complexity is in handling them *correctly* rather than nagging the user:

- **OTP-aware fallback chain** — on a background sync the server tries saved credentials, then a saved session, and distinguishes a *genuine* one-time-passcode challenge from a plain technical failure (stale selector, timeout, bot block). It only asks the user to re-login when that's actually the problem — a technical error never masquerades as "please re-authenticate."
- **Cancel on window close** — a thread-safe registry lets an in-flight headless scrape be cancelled cleanly the moment the user closes the app, instead of leaving an orphaned browser running.
- **Idempotent dedup** — each transaction's identity is a hash of `account + date + normalized description + signed amount`, with an occurrence-index tiebreaker for genuine same-day repeats. Running balance is deliberately excluded from the key because banks report it inconsistently and it would forge false duplicates. Re-syncing an account re-imports nothing already stored.
- **Quota circuit breaker** — the auto-sync scheduler trips a one-hour cooldown on a Firestore quota error instead of hammering the recovering quota with retries.
- **Data-driven currency** — currency (₹/$/€/£/¥) is detected from the amounts actually scraped and stored per account, so the UI renders the right symbol for any bank in any country with no hardcoding.

---

## Repository layout

```
src/
  serve_demo.py        HTTP server: web app + JSON API + ingest endpoints + sync scheduler
  scrape_normalize.py  Server-side parsing of raw scraped cells (dates, direction, amounts, year-fix)
  parse_bank.py        PDF statement parser (pdfplumber), config-driven per bank
  categorize.py        The 4-layer hybrid categorizer + recurrence detection
  db.py                Firestore access: accounts, transactions, dedup, currency
  auth.py              User-scoped auth (every account isolated per user)
  cloud_persistence.py Firebase Storage for raw statement files

categorizer/
  src/                 Training + inference scripts for the DistilBERT model and the
                       RapidFuzz / BERT hybrid combiner

chrome_extension/      MV3 extension: the thin harvester (content script + background sync)
config/
  banks/*.json         Per-bank PDF-parser configs (BoA, Capital One, ICICI, Discover; Chase/Amex add here)
  category_rules.json  Keyword / amount rules for the rules layer
website/               Frontend (vanilla JS / HTML / CSS, Chart.js)
tests/                 Parser drift-guard tests run against the real scrape configs
Dockerfile             Container for Cloud Run
```

---

## Tech stack

- **Backend:** Python 3.11, `http.server` (`ThreadingHTTPServer`), pdfplumber, firebase-admin / google-cloud-firestore, Playwright, keyring
- **ML:** PyTorch, Hugging Face Transformers (fine-tuned DistilBERT), RapidFuzz, pandas
- **Frontend:** vanilla JS / HTML / CSS, Chart.js
- **Extension:** Chrome Manifest V3
- **Infra:** Docker, Google Cloud Run, Firestore, Firebase Storage

---

## Run it locally

```bash
pip install -r requirements.txt
python3 src/serve_demo.py            # serves on http://127.0.0.1:8000
```

Firestore access uses Application Default Credentials (`gcloud auth application-default login`, or a service-account JSON via `GOOGLE_APPLICATION_CREDENTIALS`). To load the extension: `chrome://extensions` → Developer mode → **Load unpacked** → `chrome_extension/`.

---

## A note on data & privacy

The transaction datasets and trained model used to build and evaluate the categorizer are **intentionally excluded** from this repository, because they derive from real personal financial data. The code, configs, and pipeline are all here; the private data is not. The design keeps bank credentials in the OS keychain (or Fernet-encrypted at rest), and the login page itself is never seen by the system — the user authenticates directly with their bank.
