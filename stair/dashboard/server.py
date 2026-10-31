"""Read-only local dashboard. Binds to 127.0.0.1 only; it never sends orders."""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .data import DashboardData

log = logging.getLogger("stair.dashboard")


def make_server(data: DashboardData, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    page = resources.files(__package__).joinpath("static/index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page)
            elif path == "/api/state":
                try:
                    body = json.dumps(data.snapshot()).encode()
                except Exception as exc:  # report instead of dropping the connection
                    log.exception("state failed")
                    self._send(500, "application/json", json.dumps({"error": str(exc)}).encode())
                    return
                self._send(200, "application/json", body)
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)
