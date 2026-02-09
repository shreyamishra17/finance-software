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
