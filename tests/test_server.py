import json
import sqlite3
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import error, request

from resolution_engine.server import Handler, ResolutionService, serve

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = json.loads((ROOT / "examples/knowledge.synthetic.json").read_text())
KEY = "demo-key-123456789012345678901234567890"


class ResolutionServerTests(unittest.TestCase):
    def test_public_bind_rejected(self):
        with TemporaryDirectory() as temp, self.assertRaisesRegex(ValueError, "loopback"):
            serve(KNOWLEDGE, Path(temp) / "events.sqlite", KEY, "0.0.0.0", 8789)

    def test_authenticated_http_and_metadata_only_persistence(self):
        with TemporaryDirectory() as temp:
            db = Path(temp) / "events.sqlite"
            service = ResolutionService(KNOWLEDGE, db, KEY)
            handler = type("TestHandler", (Handler,), {"service": service})
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            except PermissionError:
                raise unittest.SkipTest("loopback sockets unavailable in sandbox") from None
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/v1/resolve"
                payload = json.dumps({"id": "private-ticket-123", "locale": "en",
                                      "text": "Where is the product user guide?"}).encode()

                def post(token=None):
                    headers = {"Content-Type": "application/json"}
                    if token:
                        headers["Authorization"] = "Bearer " + token
                    req = request.Request(url, payload, headers)
                    try:
                        with request.urlopen(req, timeout=5) as response:
                            return response.status, json.loads(response.read())
                    except error.HTTPError as exc:
                        with exc:
                            return exc.code, json.loads(exc.read())

                self.assertEqual(post()[0], 401)
                status, result = post(KEY)
                self.assertEqual(status, 200)
                self.assertEqual(result["decision"], "answer")
                with sqlite3.connect(db) as conn:
                    rows = conn.execute("SELECT ticket_hmac,decision,article_id FROM decisions").fetchall()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0][1:], ("answer", "en-product-guide"))
                    self.assertNotIn("private-ticket-123", str(rows))
                    self.assertNotIn("product user guide?", str(rows))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
