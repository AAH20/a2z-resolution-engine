import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import request

from resolution_engine.deploy import DeployStore, ZendeskClient

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = json.loads((ROOT / "examples/knowledge.synthetic.json").read_text())
TICKET = json.loads((ROOT / "examples/zendesk-ticket.synthetic.json").read_text())
KEY = "deployment-test-key-12345678901234567890"


class FakeClient:
    def __init__(self, ticket=None, fail=False):
        self.ticket = ticket or TICKET.copy()
        self.fail = fail
        self.posts = []

    def fetch(self, ticket_id):
        return self.ticket

    def post_approved_reply(self, ticket_id, answer, stamp):
        self.posts.append((ticket_id, answer, stamp))
        if self.fail:
            raise TimeoutError("outcome unknown")
        return {"id": ticket_id}


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "deploy.sqlite"
        self.store = DeployStore(self.db, KNOWLEDGE, KEY)

    def test_approve_send_once(self):
        draft = self.store.prepare(TICKET, "en")
        self.assertEqual(draft["status"], "PENDING_REVIEW")
        self.assertEqual(self.store.prepare(TICKET, "en")["draft_id"], draft["draft_id"])
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "not approved"):
            self.store.send(draft["draft_id"], 101, client)
        self.store.review(draft["draft_id"], "Reviewer A", True)
        result = self.store.send(draft["draft_id"], 101, client)
        self.assertEqual(result["status"], "SENT_UNVERIFIED")
        self.assertEqual(result["customer_acceptance"], "NOT_MEASURED")
        self.assertEqual(len(client.posts), 1)
        outcome = self.store.record_outcome(draft["draft_id"], "Reviewer B", "ACCEPTED")
        self.assertEqual(outcome["evidence_class"], "OPERATOR_DECLARED_UNVERIFIED")
        self.assertEqual(self.store.summary()["operator_declared_outcomes"], {"ACCEPTED": 1})
        self.assertIsNone(self.store.summary()["verified_accepted_resolutions"])
        with self.assertRaisesRegex(ValueError, "already recorded"):
            self.store.record_outcome(draft["draft_id"], "Reviewer B", "ACCEPTED")
        with self.assertRaises(ValueError):
            self.store.send(draft["draft_id"], 101, client)
        with sqlite3.connect(self.db) as conn:
            text = str(conn.execute("SELECT * FROM drafts").fetchall())
        self.assertNotIn("product user guide", text)
        self.assertNotIn("Where is", text)

    def test_rejection_prevents_send(self):
        draft = self.store.prepare(TICKET, "en")
        self.store.review(draft["draft_id"], "Reviewer A", False)
        with self.assertRaisesRegex(ValueError, "confirmed sent"):
            self.store.record_outcome(draft["draft_id"], "Reviewer B", "ACCEPTED")
        with self.assertRaises(ValueError):
            self.store.send(draft["draft_id"], 101, FakeClient())

    def test_stale_ticket_prevents_send(self):
        draft = self.store.prepare(TICKET, "en")
        self.store.review(draft["draft_id"], "Reviewer A", True)
        stale = FakeClient({**TICKET, "updated_at": "2026-09-24T02:00:00Z"})
        with self.assertRaisesRegex(ValueError, "ticket changed"):
            self.store.send(draft["draft_id"], 101, stale)
        self.assertEqual(stale.posts, [])

    def test_uncertain_write_never_auto_retried(self):
        draft = self.store.prepare(TICKET, "en")
        self.store.review(draft["draft_id"], "Reviewer A", True)
        client = FakeClient(fail=True)
        with self.assertRaises(TimeoutError):
            self.store.send(draft["draft_id"], 101, client)
        self.assertEqual(self.store.summary()["draft_counts"]["UNCERTAIN_RECONCILE"], 1)
        with self.assertRaises(ValueError):
            self.store.send(draft["draft_id"], 101, client)
        self.assertEqual(len(client.posts), 1)

    def test_sensitive_ticket_escalates(self):
        ticket = {**TICKET, "description": "I need a refund on my card"}
        result = self.store.prepare(ticket, "en")
        self.assertEqual(result["status"], "ESCALATE")
        self.assertEqual(self.store.summary()["draft_counts"], {})

    def test_zendesk_oauth_safe_update_shape(self):
        seen = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, *_): return b'{"ticket":{"id":101}}'

        def opener(req: request.Request, timeout):
            seen.append(req)
            return Response()

        client = ZendeskClient("example", "oauth-test", opener=opener)
        self.assertEqual(client.fetch(101)["id"], 101)
        client.post_approved_reply(101, "Approved answer", TICKET["updated_at"])
        self.assertEqual(seen[0].get_method(), "GET")
        req = seen[1]
        self.assertEqual(req.get_method(), "PUT")
        self.assertEqual(req.full_url, "https://example.zendesk.com/api/v2/tickets/101.json")
        self.assertEqual(req.get_header("Authorization"), "Bearer oauth-test")
        body = json.loads(req.data)
        self.assertEqual(body["ticket"]["comment"], {"body": "Approved answer", "public": True})
        self.assertTrue(body["ticket"]["safe_update"])

    def test_bad_subdomain_rejected(self):
        for subdomain in ("evil.com", "http://evil", "A", "localhost:80"):
            with self.assertRaises(ValueError):
                ZendeskClient(subdomain, "token")


if __name__ == "__main__":
    unittest.main()
