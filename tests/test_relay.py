import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from resolution_engine.deploy import DeployStore
from resolution_engine.relay import RelayService

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = json.loads((ROOT / "examples/knowledge.synthetic.json").read_text())
TICKET = json.loads((ROOT / "examples/zendesk-ticket.synthetic.json").read_text())


class FixtureConnector:
    def __init__(self):
        self.ticket = TICKET.copy()
        self.reads = 0

    def fetch(self, ticket_id):
        self.reads += 1
        return self.ticket


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DeployStore(Path(self.temp.name) / "relay.sqlite3", KNOWLEDGE, "local-db-key-123456789012345678901")
        self.connector = FixtureConnector()
        self.service = RelayService(self.store, "operator-token-123456789012345678901", {
            "Reviewer A": "reviewer-token-123456789012345678901"}, self.connector)
        self.operator = ("operator", "operator")
        self.reviewer = ("reviewer", "Reviewer A")

    def test_separate_roles_and_exact_preview(self):
        self.assertEqual(self.service.handle("GET", "/api/queue", None)[0], 401)
        self.assertEqual(self.service.handle("POST", "/api/prepare", self.reviewer, {"ticket_id": 101, "locale": "en"})[0], 403)
        code, result = self.service.handle("POST", "/api/prepare", self.operator, {"ticket_id": 101, "locale": "en"})
        self.assertEqual(code, 200)
        draft_id = result["draft"]["draft_id"]
        self.assertEqual(self.service.handle("POST", "/api/review", self.operator, {"draft_id": draft_id, "decision": "approve"})[0], 403)
        self.assertEqual(self.service.handle("POST", "/api/review", self.reviewer, {"draft_id": draft_id, "decision": "approve"})[0], 409)
        self.assertEqual(self.service.handle("POST", "/api/preview", self.reviewer, {"ticket_id": 101, "locale": "en", "draft_id": draft_id})[0], 200)
        code, review = self.service.handle("POST", "/api/review", self.reviewer, {"draft_id": draft_id, "decision": "approve"})
        self.assertEqual((code, review["status"]), (200, "APPROVED_PENDING_SEND"))
        queue = self.service.handle("GET", "/api/queue", self.reviewer)[1]
        self.assertEqual(queue["drafts"][0]["reviewer"], "Reviewer A")
        self.assertNotIn("description", str(queue))
        self.assertNotIn("answer", str(queue))
        self.assertFalse(any("send" in path for path in ("/api/queue", "/api/prepare", "/api/preview", "/api/review")))

    def test_changed_ticket_blocks_preview(self):
        draft = self.service.handle("POST", "/api/prepare", self.operator, {"ticket_id": 101, "locale": "en"})[1]["draft"]
        self.connector.ticket = {**TICKET, "updated_at": "2026-09-24T02:00:00Z"}
        code, _ = self.service.handle("POST", "/api/preview", self.reviewer, {"ticket_id": 101, "locale": "en", "draft_id": draft["draft_id"]})
        self.assertEqual(code, 409)
        self.assertEqual(self.service.handle("POST", "/api/review", self.reviewer, {"draft_id": draft["draft_id"], "decision": "approve"})[0], 409)

    def test_token_configuration_requires_distinct_long_secrets(self):
        with self.assertRaises(ValueError):
            RelayService(self.store, "short", {"A": "reviewer-token-123456789012345678901"})
        with self.assertRaises(ValueError):
            RelayService(self.store, "same-token-1234567890123456789012", {"A": "same-token-1234567890123456789012"})


if __name__ == "__main__":
    unittest.main()
