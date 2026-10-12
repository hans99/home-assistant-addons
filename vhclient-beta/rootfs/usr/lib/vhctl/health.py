"""HTTP health endpoint for Home Assistant Supervisor watchdog."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Tuple
from urllib.parse import urlparse

from . import __version__, pipe

log = logging.getLogger("vhctl")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8199


def check_health() -> Tuple[bool, dict]:
    """Return (ok, details) for VirtualHere client IPC liveness."""
    details = {
        "app": "vhclient-beta",
        "version": __version__,
        "pipes": pipe.pipes_ready(),
    }
    if not details["pipes"]:
        details["error"] = "VirtualHere IPC pipes not present"
        return False, details

    try:
        # Pipe-only: do not fall back to `vhclient -t` (can mask a dead IPC path).
        result = pipe.send_command("LIST", timeout=2.0, allow_t_fallback=False)
    except pipe.PipeError as exc:
        details["error"] = str(exc)
        return False, details

    details["list_ok"] = bool(result.ok) and not result.failed
    if not details["list_ok"]:
        details["error"] = result.text or "LIST failed"
        return False, details

    details["status"] = "ok"
    return True, details


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("health: " + fmt, *args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path not in ("/health", "/"):
            self._send(404, {"status": "not_found"})
            return
        ok, details = check_health()
        self._send(200 if ok else 503, details)

    def do_HEAD(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path not in ("/health", "/"):
            self.send_response(404)
            self.end_headers()
            return
        ok, _ = check_health()
        self.send_response(200 if ok else 503)
        self.end_headers()


def start_health_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Start the watchdog HTTP server in a daemon thread and return it."""
    server = ThreadingHTTPServer((host, port), _HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="vhctl-health",
        daemon=True,
    )
    thread.start()
    log.info("Watchdog health endpoint listening on http://%s:%s/health", host, port)
    return server
