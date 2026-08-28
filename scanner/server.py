"""Dashboard web a zero dipendenze (solo standard library).

Serve la pagina e quattro endpoint JSON. Ascolta su 0.0.0.0 cosi' la apri sia
dal Mac sia dall'iPhone sulla stessa rete: http://<ip-del-mac>:8787
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def _encode(obj):
    if is_dataclass(obj):
        return asdict(obj)
    raise TypeError(f"non serializzabile: {type(obj)}")


class DashboardState:
    """Stato condiviso tra il thread di scan e il server HTTP."""

    def __init__(self, scanner, store, config: dict):
        self.scanner = scanner
        self.store = store
        self.config = config


class _Handler(BaseHTTPRequestHandler):
    state: Optional[DashboardState] = None
    protocol_version = "HTTP/1.1"

    # Il log di default sporca la console dello scanner a ogni polling.
    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass       # la pagina si e' chiusa mentre rispondevamo

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, default=_encode, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:                     # noqa: N802 (nome imposto da BaseHTTPRequestHandler)
        path = urlparse(self.path).path
        st = self.state
        if st is None:
            self._send_json({"error": "stato non inizializzato"}, 503)
            return

        if path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")
        elif path == "/api/scan":
            result = st.scanner.latest
            if result is None:
                self._send_json({"ts": None, "opportunities": [], "pending": True})
            else:
                payload = asdict(result)
                payload["config"] = {
                    "signal_score": st.config.get("signal_score"),
                    "alert_score": st.config.get("alert_score"),
                    "min_display_score": st.config.get("min_display_score"),
                    "scan_interval_seconds": st.config.get("scan_interval_seconds"),
                }
                self._send_json(payload)
        elif path == "/api/signals":
            self._send_json(st.store.recent_signals(limit=80) if st.store else [])
        elif path == "/api/performance":
            if not st.store:
                self._send_json({"summary": {}, "buckets": []})
            else:
                self._send_json(
                    {"summary": st.store.summary(), "buckets": st.store.performance(240)}
                )
        elif path == "/api/health":
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, 404)

    def _serve_file(self, name: str, content_type: str) -> None:
        # Nessun path traversal: i nomi arrivano solo da questo modulo.
        full = os.path.join(WEB_DIR, name)
        try:
            with open(full, "rb") as fh:
                self._send(200, fh.read(), content_type)
        except OSError:
            self._send_json({"error": f"file mancante: {name}"}, 404)


def local_ip() -> str:
    """IP LAN del Mac, per stampare l'indirizzo da aprire sull'iPhone."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))     # nessun pacchetto inviato: serve solo la rotta
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def make_server(state: DashboardState, host: str = "0.0.0.0", port: int = 8787) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
