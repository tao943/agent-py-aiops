"""Minimal deterministic HTTP upstream used only by the live-eval profile."""

from __future__ import annotations

import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._respond(HTTPStatus.OK, {"status": "healthy"})
            return
        if parsed.path != "/slow":
            self._respond(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return
        raw_delay = parse_qs(parsed.query).get("delay_ms", ["0"])[0]
        try:
            delay_ms = min(max(int(raw_delay), 0), 5_000)
        except ValueError:
            self._respond(HTTPStatus.BAD_REQUEST, {"status": "invalid_delay"})
            return
        time.sleep(delay_ms / 1_000)
        self._respond(HTTPStatus.OK, {"status": "completed", "delayMs": delay_ms})

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _respond(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
