"""Human-reviewed Zendesk deployment path for the extractive resolution engine.

Only an explicitly approved draft can be sent. An uncertain network result is
never retried automatically: a human must reconcile it in Zendesk first.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib import request

from .engine import canonical_hash, resolve, validate_knowledge


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ticket_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Zendesk ticket id must be a positive integer")
    return str(value)


def _stamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("ticket updated_at is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("ticket updated_at needs a timezone")
    return value


class ZendeskClient:
    """Narrow OAuth ticket client; never accepts arbitrary remote URLs."""

    def __init__(self, subdomain: str, access_token: str, *, opener=None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,61}[a-z0-9]?", subdomain):
            raise ValueError("invalid Zendesk subdomain")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Zendesk OAuth access token is required")
        self.base = f"https://{subdomain}.zendesk.com/api/v2/tickets"
        self.token = access_token
        self.opener = opener or request.urlopen

    def _call(self, method: str, ticket_id: int, payload: dict | None = None) -> dict:
        url = f"{self.base}/{_ticket_id(ticket_id)}.json"
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = request.Request(url, data=data, method=method,
                              headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                                       "Content-Type": "application/json"})
        with self.opener(req, timeout=10) as response:
            body = json.load(response)
        if not isinstance(body, dict) or not isinstance(body.get("ticket"), dict):
            raise ValueError("invalid Zendesk response")
        return body["ticket"]

    def fetch(self, ticket_id: int) -> dict:
        return self._call("GET", ticket_id)

    def post_approved_reply(self, ticket_id: int, answer: str, updated_stamp: str) -> dict:
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("approved reply cannot be empty")
        return self._call("PUT", ticket_id, {"ticket": {"safe_update": True,
            "updated_stamp": _stamp(updated_stamp), "comment": {"body": answer, "public": True}}})


class DeployStore:
    def __init__(self, db: Path, snapshot: dict, key: str):
        if not isinstance(key, str) or len(key) < 32:
            raise ValueError("local key must be at least 32 characters")
        validate_knowledge(snapshot, datetime.now(UTC).date())
        self.db, self.snapshot, self.key = db, snapshot, key
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS drafts (
                draft_id TEXT PRIMARY KEY, ticket_hmac TEXT NOT NULL, source_stamp TEXT NOT NULL,
                knowledge_sha256 TEXT NOT NULL, answer_sha256 TEXT NOT NULL,
                article_id TEXT NOT NULL, status TEXT NOT NULL, reviewer TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id TEXT NOT NULL, at TEXT NOT NULL,
                event TEXT NOT NULL, actor TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS outcomes (
                draft_id TEXT PRIMARY KEY, outcome TEXT NOT NULL, reviewer TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )""")
        os.chmod(db, 0o600)

    def _hmac(self, ticket_id: int) -> str:
        return hmac.digest(self.key.encode(), _ticket_id(ticket_id).encode(), "sha256").hex()

    def _row(self, draft_id: str) -> dict:
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if row is None:
            raise ValueError("draft not found")
        return dict(row)

    def prepare(self, ticket: dict, locale: str) -> dict:
        if not isinstance(ticket, dict):
            raise ValueError("ticket must be an object")
        ticket_id = ticket.get("id")
        ticket_hmac = self._hmac(ticket_id)
        stamp = _stamp(ticket.get("updated_at"))
        description = ticket.get("description")
        if not isinstance(description, str):
            raise ValueError("ticket description must be text")
        result = resolve(self.snapshot, {"id": _ticket_id(ticket_id), "locale": locale, "text": description})
        if result["decision"] != "answer":
            return {"status": "ESCALATE", "reason": result["reason"], "ticket_hmac": ticket_hmac}
        answer_hash = hashlib.sha256(result["answer"].encode()).hexdigest()
        draft_id = canonical_hash({"ticket_hmac": ticket_hmac, "source_stamp": stamp,
                                   "knowledge_sha256": result["knowledge_sha256"], "answer_sha256": answer_hash})
        with closing(sqlite3.connect(self.db)) as conn, conn:
            existing = conn.execute("SELECT status FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
            if existing is None:
                conn.execute("""INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?)""",
                             (draft_id, ticket_hmac, stamp, result["knowledge_sha256"], answer_hash,
                              result["article_id"], "PENDING_REVIEW", None, _now(), _now()))
                conn.execute("INSERT INTO events(draft_id,at,event,actor) VALUES (?,?,?,?)",
                             (draft_id, _now(), "PREPARED", "engine"))
        return {"status": existing[0] if existing else "PENDING_REVIEW", "draft_id": draft_id,
                "ticket_hmac": ticket_hmac, "answer": result["answer"], "source": result["source"],
                "article_id": result["article_id"], "source_stamp": stamp,
                "claim_boundary": "Draft only; not sent to customer or verified as resolution."}

    def review(self, draft_id: str, reviewer: str, approve: bool) -> dict:
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("named reviewer required")
        status = "APPROVED_PENDING_SEND" if approve else "REJECTED"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            changed = conn.execute("UPDATE drafts SET status=?, reviewer=?, updated_at=? WHERE draft_id=? AND status='PENDING_REVIEW'",
                                   (status, reviewer.strip(), _now(), draft_id)).rowcount
            if not changed:
                raise ValueError("draft missing or not pending review")
            conn.execute("INSERT INTO events(draft_id,at,event,actor) VALUES (?,?,?,?)",
                         (draft_id, _now(), status, reviewer.strip()))
        return {"draft_id": draft_id, "status": status, "reviewer": reviewer.strip()}

    def send(self, draft_id: str, ticket_id: int, client: ZendeskClient) -> dict:
        row = self._row(draft_id)
        if row["status"] != "APPROVED_PENDING_SEND" or not hmac.compare_digest(row["ticket_hmac"], self._hmac(ticket_id)):
            raise ValueError("draft not approved for this ticket")
        if row["knowledge_sha256"] != canonical_hash(self.snapshot):
            raise ValueError("knowledge changed since review")
        article = next((a for a in self.snapshot["articles"] if a["id"] == row["article_id"]), None)
        if article is None or hashlib.sha256(article["answer"].encode()).hexdigest() != row["answer_sha256"]:
            raise ValueError("approved answer no longer matches")
        current = client.fetch(ticket_id)
        if _stamp(current.get("updated_at")) != row["source_stamp"]:
            raise ValueError("ticket changed since draft; prepare a new draft")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            changed = conn.execute("UPDATE drafts SET status='SENDING', updated_at=? WHERE draft_id=? AND status='APPROVED_PENDING_SEND'",
                                   (_now(), draft_id)).rowcount
            if not changed:
                raise ValueError("draft already claimed for send")
            conn.execute("INSERT INTO events(draft_id,at,event,actor) VALUES (?,?,?,?)",
                         (draft_id, _now(), "SENDING", row["reviewer"]))
        try:
            posted = client.post_approved_reply(ticket_id, article["answer"], row["source_stamp"])
        except Exception:
            # A timeout may occur after Zendesk accepted the write; do not retry.
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE drafts SET status='UNCERTAIN_RECONCILE', updated_at=? WHERE draft_id=? AND status='SENDING'", (_now(), draft_id))
                conn.execute("INSERT INTO events(draft_id,at,event,actor) VALUES (?,?,?,?)",
                             (draft_id, _now(), "UNCERTAIN_RECONCILE", "connector"))
            raise
        if _ticket_id(posted.get("id")) != _ticket_id(ticket_id):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE drafts SET status='UNCERTAIN_RECONCILE', updated_at=? WHERE draft_id=?", (_now(), draft_id))
            raise ValueError("Zendesk response ticket id mismatch; reconcile manually")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE drafts SET status='SENT_UNVERIFIED', updated_at=? WHERE draft_id=? AND status='SENDING'", (_now(), draft_id))
            conn.execute("INSERT INTO events(draft_id,at,event,actor) VALUES (?,?,?,?)",
                         (draft_id, _now(), "SENT_UNVERIFIED", "connector"))
        return {"draft_id": draft_id, "status": "SENT_UNVERIFIED", "ticket_hmac": row["ticket_hmac"],
                "customer_acceptance": "NOT_MEASURED"}

    def record_outcome(self, draft_id: str, reviewer: str, outcome: str) -> dict:
        if outcome not in ("ACCEPTED", "REWORK", "UNRESOLVED"):
            raise ValueError("invalid outcome")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("named outcome reviewer required")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            row = conn.execute("SELECT status FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
            if row is None or row[0] != "SENT_UNVERIFIED":
                raise ValueError("outcome requires a confirmed sent draft")
            try:
                conn.execute("INSERT INTO outcomes VALUES (?,?,?,?)", (draft_id, outcome, reviewer.strip(), _now()))
            except sqlite3.IntegrityError:
                raise ValueError("outcome already recorded; correction workflow not implemented") from None
            conn.execute("INSERT INTO events(draft_id,at,event,actor) VALUES (?,?,?,?)",
                         (draft_id, _now(), f"OUTCOME_{outcome}", reviewer.strip()))
        return {"draft_id": draft_id, "outcome": outcome,
                "evidence_class": "OPERATOR_DECLARED_UNVERIFIED"}

    def summary(self) -> dict:
        with closing(sqlite3.connect(self.db)) as conn:
            counts = dict(conn.execute("SELECT status,COUNT(*) FROM drafts GROUP BY status").fetchall())
            outcomes = dict(conn.execute("SELECT outcome,COUNT(*) FROM outcomes GROUP BY outcome").fetchall())
        return {"draft_counts": counts, "operator_declared_outcomes": outcomes,
                "verified_accepted_resolutions": None,
                "claim_boundary": "Operator declarations are not authenticated customer outcomes or independently verified resolutions."}

    def list_drafts(self, limit: int = 100) -> list[dict]:
        """Metadata-only queue; ticket text and IDs are never returned from storage."""
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""SELECT draft_id,status,article_id,source_stamp,reviewer,created_at,updated_at
                                   FROM drafts ORDER BY created_at DESC,draft_id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(row) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Human-reviewed Zendesk deployment path")
    parser.add_argument("--knowledge", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--key-env", default="RESOLUTION_ENGINE_KEY")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--ticket-file", required=True, type=Path)
    prepare.add_argument("--locale", choices=("ar", "en"), required=True)
    fetch_prepare = sub.add_parser("fetch-prepare")
    fetch_prepare.add_argument("--ticket-id", type=int, required=True)
    fetch_prepare.add_argument("--subdomain", required=True)
    fetch_prepare.add_argument("--oauth-token-env", default="ZENDESK_OAUTH_TOKEN")
    fetch_prepare.add_argument("--locale", choices=("ar", "en"), required=True)
    review = sub.add_parser("review")
    review.add_argument("--draft-id", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--decision", choices=("approve", "reject"), required=True)
    send = sub.add_parser("send")
    send.add_argument("--draft-id", required=True)
    send.add_argument("--ticket-id", type=int, required=True)
    send.add_argument("--subdomain", required=True)
    send.add_argument("--oauth-token-env", default="ZENDESK_OAUTH_TOKEN")
    outcome = sub.add_parser("outcome")
    outcome.add_argument("--draft-id", required=True)
    outcome.add_argument("--reviewer", required=True)
    outcome.add_argument("--status", choices=("ACCEPTED", "REWORK", "UNRESOLVED"), required=True)
    sub.add_parser("summary")
    args = parser.parse_args()
    store = DeployStore(args.db, json.loads(args.knowledge.read_text(encoding="utf-8")), os.environ.get(args.key_env, ""))
    if args.command == "prepare":
        result = store.prepare(json.loads(args.ticket_file.read_text(encoding="utf-8")), args.locale)
    elif args.command == "fetch-prepare":
        client = ZendeskClient(args.subdomain, os.environ.get(args.oauth_token_env, ""))
        result = store.prepare(client.fetch(args.ticket_id), args.locale)
    elif args.command == "review":
        result = store.review(args.draft_id, args.reviewer, args.decision == "approve")
    elif args.command == "send":
        result = store.send(args.draft_id, args.ticket_id,
                            ZendeskClient(args.subdomain, os.environ.get(args.oauth_token_env, "")))
    elif args.command == "outcome":
        result = store.record_outcome(args.draft_id, args.reviewer, args.status)
    else:
        result = store.summary()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
