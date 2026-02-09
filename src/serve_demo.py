#!/usr/bin/env python3
from __future__ import annotations

import cgi
import json
import tempfile
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from parse_boa_pdf import parse_pdf_to_payload


ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "website"


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.path = "/about.html"
        return super().do_GET()

    def do_POST(self) -> None:
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

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": ctype,
                "CONTENT_LENGTH": str(content_len),
            },
        )
        if "file" not in form:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'file' field"})
            return

        uploaded = form["file"]
        if not getattr(uploaded, "file", None):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "No file uploaded"})
            return

        file_name = (uploaded.filename or "").strip()
        if file_name and not file_name.lower().endswith(".pdf"):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Please upload a PDF file"})
            return

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(uploaded.file.read())
            tmp_path = Path(tmp.name)

        try:
            payload = parse_pdf_to_payload(tmp_path)
        except Exception as exc:  # noqa: BLE001
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

        self._send_json(HTTPStatus.OK, payload)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 8000), DemoHandler)
    print("Serving Finely demo on http://127.0.0.1:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
