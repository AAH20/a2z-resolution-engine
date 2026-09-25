import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import error, request

from resolution_engine.deploy import DeployStore
from resolution_engine.relay import RelayService, handler_for

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = json.loads((ROOT / "examples/knowledge.synthetic.json").read_text())
TICKET = json.loads((ROOT / "examples/zendesk-ticket.synthetic.json").read_text())
OPERATOR = "operator-token-123456789012345678901"
REVIEWER = "reviewer-token-123456789012345678901"


class DemoConnector:
    def fetch(self, ticket_id):
        if ticket_id != 101:
            raise ValueError("unknown ticket")
        return TICKET


class RelayHttpTests(unittest.TestCase):
    def test_loopback_review_flow(self):
        with TemporaryDirectory() as temp:
            store = DeployStore(Path(temp) / "pilot.sqlite3", KNOWLEDGE, "local-db-key-123456789012345678901")
            service = RelayService(store, OPERATOR, {"Reviewer A": REVIEWER}, DemoConnector())
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(service))
            except OSError:
                self.skipTest("loopback sockets unavailable in sandbox")
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            base = f"http://127.0.0.1:{server.server_port}"

            def call(path, token=None, payload=None):
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                req = request.Request(base + path, data=json.dumps(payload).encode() if payload is not None else None,
                                      headers=headers, method="POST" if payload is not None else "GET")
                try:
                    with request.urlopen(req, timeout=3) as response:
                        return response.status, json.load(response)
                except error.HTTPError as exc:
                    with exc:
                        return exc.code, json.load(exc)

            self.assertEqual(call("/api/queue")[0], 401)
            code, prepared = call("/api/prepare", OPERATOR, {"ticket_id": 101, "locale": "en"})
            self.assertEqual(code, 200)
            draft = prepared["draft"]["draft_id"]
            self.assertEqual(call("/api/review", REVIEWER, {"draft_id": draft, "decision": "approve"})[0], 409)
            self.assertEqual(call("/api/preview", REVIEWER, {"draft_id": draft, "ticket_id": 101, "locale": "en"})[0], 200)
            self.assertEqual(call("/api/review", REVIEWER, {"draft_id": draft, "decision": "approve"})[1]["status"], "APPROVED_PENDING_SEND")
            self.assertEqual(call("/api/send", REVIEWER, {"draft_id": draft})[0], 404)


if __name__ == "__main__":
    unittest.main()
