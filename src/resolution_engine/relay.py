"""Loopback-only Resolution Relay operator console.

Separate operator and reviewer bearer credentials are used for local pilots.
The browser never receives a Zendesk token and this service has no send endpoint.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .deploy import DeployStore, ZendeskClient

MAX_BODY = 16 * 1024
HTML = Path(__file__).with_name("relay.html")


def _credential(value: str, name: str) -> str:
    if not isinstance(value, str) or len(value) < 32:
        raise ValueError(f"{name} must be at least 32 characters")
    return value


class RelayService:
    def __init__(self, store: DeployStore, operator_token: str, reviewers: dict[str, str], zendesk: ZendeskClient | None = None):
        self.store = store
        self.operator_token = _credential(operator_token, "operator token")
        if not isinstance(reviewers, dict) or not reviewers or any(not isinstance(name, str) or not name.strip() or not isinstance(token, str) for name, token in reviewers.items()):
            raise ValueError("named reviewers and tokens are required")
        self.reviewers = {name: _credential(token, f"reviewer token {name}") for name, token in reviewers.items()}
        if len(set([operator_token, *self.reviewers.values()])) != 1 + len(self.reviewers):
            raise ValueError("operator and reviewer tokens must be distinct")
        self.zendesk = zendesk
        self.previewed: set[tuple[str, str]] = set()

    def actor(self, header: str | None) -> tuple[str, str] | None:
        if not header or not header.startswith("Bearer "):
            return None
        supplied = header[7:]
        if not supplied.isascii():
            return None
        if hmac.compare_digest(supplied, self.operator_token):
            return "operator", "operator"
        for name, token in self.reviewers.items():
            if hmac.compare_digest(supplied, token):
                return "reviewer", name
        return None

    def handle(self, method: str, path: str, actor: tuple[str, str] | None, payload: dict | None = None) -> tuple[int, dict]:
        if actor is None:
            return 401, {"error": "authentication required"}
        role, name = actor
        if method == "GET" and path == "/api/queue":
            return 200, {"drafts": self.store.list_drafts(), "summary": self.store.summary()}
        if method == "POST" and path == "/api/prepare":
            if role != "operator":
                return 403, {"error": "operator role required"}
            if not self.zendesk:
                return 503, {"error": "Zendesk connector not configured"}
            if not isinstance(payload, dict) or set(payload) != {"ticket_id", "locale"} or type(payload["ticket_id"]) is not int or payload["locale"] not in ("ar", "en"):
                return 400, {"error": "ticket_id and locale required"}
            ticket = self.zendesk.fetch(payload["ticket_id"])
            result = self.store.prepare(ticket, payload["locale"])
            return 200, {"draft": result, "ticket_description": ticket.get("description") if result["status"] == "PENDING_REVIEW" else None}
        if method == "POST" and path == "/api/review":
            if role != "reviewer":
                return 403, {"error": "reviewer role required"}
            if not isinstance(payload, dict) or set(payload) != {"draft_id", "decision"} or not isinstance(payload["draft_id"], str) or payload["decision"] not in ("approve", "reject"):
                return 400, {"error": "draft_id and approve/reject decision required"}
            if payload["decision"] == "approve" and (name, payload["draft_id"]) not in self.previewed:
                return 409, {"error": "reviewer must preview the exact current ticket and draft first"}
            self.previewed.discard((name, payload["draft_id"]))
            return 200, self.store.review(payload["draft_id"], name, payload["decision"] == "approve")
        if method == "POST" and path == "/api/preview":
            if role != "reviewer":
                return 403, {"error": "reviewer role required"}
            if not self.zendesk:
                return 503, {"error": "Zendesk connector not configured"}
            if not isinstance(payload, dict) or set(payload) != {"ticket_id", "locale", "draft_id"} or type(payload["ticket_id"]) is not int or payload["locale"] not in ("ar", "en") or not isinstance(payload["draft_id"], str):
                return 400, {"error": "ticket_id, locale, and draft_id required"}
            ticket = self.zendesk.fetch(payload["ticket_id"])
            draft = self.store.prepare(ticket, payload["locale"])
            if draft.get("draft_id") != payload["draft_id"] or draft.get("status") != "PENDING_REVIEW":
                return 409, {"error": "ticket changed or draft is no longer pending"}
            self.previewed.add((name, payload["draft_id"]))
            return 200, {"draft": draft, "ticket_description": ticket.get("description")}
        return 404, {"error": "not found"}


def handler_for(service: RelayService):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log bearer tokens or ticket content.

        def _json(self, code: int, value: dict):
            raw = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/":
                raw = HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.path == "/relay.js":
                raw = HTML.with_name("relay.js").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(raw)
                return
            code, result = service.handle("GET", self.path, service.actor(self.headers.get("Authorization")))
            self._json(code, result)

        def do_POST(self):
            if self.headers.get("Origin") not in (None, f"http://{self.headers.get('Host')}"):
                self._json(403, {"error": "cross-origin request rejected"})
                return
            try:
                size = int(self.headers.get("Content-Length", "-1"))
                if size < 0 or size > MAX_BODY:
                    self._json(413, {"error": "request too large"})
                    return
                payload = json.loads(self.rfile.read(size))
                code, result = service.handle("POST", self.path, service.actor(self.headers.get("Authorization")), payload)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                code, result = 400, {"error": str(exc)}
            except Exception:
                code, result = 502, {"error": "connector or store error; inspect server locally"}
            self._json(code, result)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Resolution Relay local operator console")
    parser.add_argument("--knowledge", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    connector = parser.add_mutually_exclusive_group(required=True)
    connector.add_argument("--subdomain")
    connector.add_argument("--ticket-file", type=Path, help="synthetic local demo ticket; never enables a send")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    store = DeployStore(args.db, json.loads(args.knowledge.read_text()), os.environ.get("RESOLUTION_ENGINE_KEY", ""))
    reviewers = json.loads(os.environ.get("RELAY_REVIEWER_TOKENS", "{}"))
    if args.ticket_file:
        fixture = json.loads(args.ticket_file.read_text())

        class DemoClient:
            def fetch(self, ticket_id: int) -> dict:
                if type(ticket_id) is not int or ticket_id != fixture.get("id"):
                    raise ValueError("ticket not found in local fixture")
                return fixture

        client = DemoClient()
    else:
        client = ZendeskClient(args.subdomain, os.environ.get("ZENDESK_OAUTH_TOKEN", ""))
    service = RelayService(store, os.environ.get("RELAY_OPERATOR_TOKEN", ""), reviewers, client)
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(service)) as server:
        print(f"Resolution Relay listening on http://127.0.0.1:{args.port}/", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
