#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
import webbrowser
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from categorize import categorize_transactions
from parse_boa_pdf import parse_pdf_to_payload, persist_uploaded_statement


ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "website"


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def _add_cors_headers(self) -> None:
        # Allow Cloudflare Pages (or any frontend) to call this API.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._add_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        # Serve the static site from the website/ folder.
        path = urlparse(self.path).path
        if path == "/":
            self.path = "/about.html"
        return super().do_GET()

    def do_OPTIONS(self) -> None:
        # Preflight handler for cross-origin POST requests.
        path = urlparse(self.path).path
        if path != "/api/parse":
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._add_cors_headers()
        self.end_headers()

    def do_POST(self) -> None:
        # Parse a PDF statement and return structured JSON.
        path = urlparse(self.path).path
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
            payload = parse_pdf_to_payload(tmp_path)
            payload["transactions"] = categorize_transactions(payload.get("transactions", []))
            persist_mode = os.environ.get("PERSIST_MODE", "local").strip().lower()
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

    def _log_category_summary(self, transactions: list[dict]) -> None:
        counts: dict[str, int] = {}
        for txn in transactions:
            category = txn.get("category") or "Uncategorized"
            counts[category] = counts.get(category, 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"DEBUG: category counts: {summary}")


def main() -> int:
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
