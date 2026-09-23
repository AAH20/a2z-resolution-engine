"""Authenticated loopback API with metadata-only decision events."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .engine import resolve, validate_knowledge

MAX_BODY = 16_384


class ResolutionService:
    def __init__(self, snapshot: dict, db: Path, key: str):
        if not isinstance(key, str) or len(key) < 32:
            raise ValueError("gateway key must have at least 32 characters")
        validate_knowledge(snapshot, datetime.now(UTC).date())
        self.snapshot = snapshot
        self.db = db
        self.key = key
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                ticket_hmac TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                article_id TEXT,
                latency_ms INTEGER NOT NULL,
                knowledge_sha256 TEXT NOT NULL
            )""")

    def decide(self, ticket: dict) -> dict:
        start = time.monotonic()
        result = resolve(self.snapshot, ticket)
        ticket_hash = hmac.digest(self.key.encode(), result["ticket_id"].encode(), "sha256").hex()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO decisions(at,ticket_hmac,decision,reason,article_id,latency_ms,knowledge_sha256) VALUES (?,?,?,?,?,?,?)",
                         (datetime.now(UTC).isoformat(), ticket_hash, result["decision"], result["reason"],
                          result["article_id"], round((time.monotonic() - start) * 1000), result["knowledge_sha256"]))
        return result


class Handler(BaseHTTPRequestHandler):
    service: ResolutionService

    def _reply(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._reply(200, {"status": "ok"})
        else:
            self._reply(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/v1/resolve":
            self._reply(404, {"error": "not_found"})
            return
        if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.service.key):
            self._reply(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("invalid body length")
            body = json.loads(self.rfile.read(length))
            result = self.service.decide(body)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._reply(400, {"error": "invalid_request"})
            return
        except sqlite3.Error:
            self._reply(503, {"error": "decision_unavailable"})
            return
        self._reply(200, result)

    def log_message(self, format, *args):
        # HTTP access logs can expose support data in future routes; keep them disabled.
        pass


def serve(snapshot: dict, db: Path, key: str, host: str, port: int) -> None:
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback or not 1 <= port <= 65535:
        raise ValueError("built-in server requires a loopback IP and valid port")
    service = ResolutionService(snapshot, db, key)
    handler = type("ConfiguredResolutionHandler", (Handler,), {"service": service})
    with ThreadingHTTPServer((host, port), handler) as server:
        server.daemon_threads = True
        print(f"Resolution Engine listening on {host}:{port}")
        server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Resolution Engine API")
    parser.add_argument("--knowledge", required=True, type=Path)
    parser.add_argument("--db", type=Path, default=Path("decision-events.sqlite3"))
    parser.add_argument("--key-env", default="RESOLUTION_ENGINE_KEY")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8789)
    args = parser.parse_args()
    key = os.environ.get(args.key_env, "")
    serve(json.loads(args.knowledge.read_text()), args.db, key, args.host, args.port)


if __name__ == "__main__":
    main()
