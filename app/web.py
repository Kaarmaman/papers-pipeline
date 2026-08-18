from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
PORT = int(os.getenv("PORT", "8099"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(200, "application/json", json.dumps({"status": "ok"}))
            return
        if self.path == "/api/latest":
            self._serve(DATA_DIR / "reports" / "latest.json", "application/json")
            return
        if self.path in {"/", "/index.html"}:
            self._serve(DATA_DIR / "reports" / "latest.html", "text/html; charset=utf-8")
            return
        self._send(404, "text/plain; charset=utf-8", "not found")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _serve(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self._send(503, "text/plain; charset=utf-8", "report is not ready; the worker has not completed its first run")
            return
        self._send(200, content_type, body)

    def _send(self, status: int, content_type: str, body: bytes | str) -> None:
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

