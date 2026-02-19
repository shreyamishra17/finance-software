# Finance_Software
A personal finance software for intelligent finance and business management

## Run Demo

1. From this folder, run:

```bash
python3 src/serve_demo.py
```

2. Open:

```text
http://127.0.0.1:8000/demo.html
```

3. Upload a PDF statement in the demo page to parse and display results.

### Notes

- The parser API endpoint is `POST /api/parse`.
- If you open `demo.html` via a static server, parsing will fail (for example HTTP 501/404) because `/api/parse` will not exist.

## Persistence Modes

By default the demo uses local persistence.

- `PERSIST_MODE=local`:
  - Saves PDF + CSV under `my_finance/<username>/{raw,processed}`.
- `PERSIST_MODE=firebase_storage` (or `firebase`):
  - Stores files in Firebase Storage only.
  - Folder structure in bucket:
    - `<username>/raw/<statement>.pdf`
    - `<username>/processed/acc_<account_last4>.csv`
  - CSV is appended with dedupe on upload.

### Firebase Storage Setup

1. Create Firebase project and enable Firebase Storage.
2. Set service account credentials:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=\"/absolute/path/to/service-account.json\"
```

3. Set your storage bucket:

```bash
export FIREBASE_STORAGE_BUCKET=\"your-project-id.appspot.com\"
```

4. Run server in Firebase Storage mode:

```bash
PERSIST_MODE=firebase_storage python3 src/serve_demo.py
```
