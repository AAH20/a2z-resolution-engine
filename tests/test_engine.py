import json
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path

from resolution_engine.engine import resolve
from resolution_engine.evaluate import evaluate

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = json.loads((ROOT / "examples/knowledge.synthetic.json").read_text())
SUITE = json.loads((ROOT / "examples/cases.synthetic.json").read_text())
TODAY = date(2026, 9, 24)


class ResolutionEngineTests(unittest.TestCase):
    def test_approved_answer_is_verbatim_and_has_source(self):
        result = resolve(KNOWLEDGE, {"id": "one", "locale": "ar", "text": "ازاي اشوف دليل استخدام المنتج"}, today=TODAY)
        self.assertEqual(result["decision"], "answer")
        self.assertEqual(result["answer"], KNOWLEDGE["articles"][1]["answer"])
        self.assertEqual(result["source"], KNOWLEDGE["articles"][1]["source"])
        self.assertEqual(result["customer_acceptance"], "NOT_MEASURED")

    def test_account_unknown_expired_and_ambiguous_cases_escalate(self):
        sensitive = resolve(KNOWLEDGE, {"id": "two", "locale": "en", "text": "Refund my card"}, today=TODAY)
        self.assertEqual(sensitive["reason"], "account_or_payment_action")
        unknown = resolve(KNOWLEDGE, {"id": "three", "locale": "en", "text": "Does your app support Klingon?"}, today=TODAY)
        self.assertEqual(unknown["reason"], "no_grounded_answer")
        expired = resolve(KNOWLEDGE, {"id": "four", "locale": "en", "text": "What is the retired return policy?"}, today=TODAY)
        self.assertEqual(expired["decision"], "escalate")
        altered = deepcopy(KNOWLEDGE)
        duplicate = deepcopy(altered["articles"][0])
        duplicate["id"] = "en-product-guide-alt"
        altered["articles"].append(duplicate)
        ambiguous = resolve(altered, {"id": "five", "locale": "en", "text": "Where can I find the product user guide?"}, today=TODAY)
        self.assertEqual(ambiguous["reason"], "ambiguous_knowledge")

    def test_suite_reports_declared_not_real_economics(self):
        report = evaluate(KNOWLEDGE, SUITE)
        self.assertEqual(report["correct_against_declared_labels"], 6)
        self.assertEqual(report["declared_accepted_answers"], 2)
        self.assertEqual(report["modeled_total_cost_usd"], "4.62")
        self.assertEqual(report["modeled_cost_per_declared_accepted_answer_usd"], "2.3100")

    def test_invalid_input_and_knowledge_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "ticket ID"):
            resolve(KNOWLEDGE, {"id": "../x", "locale": "en", "text": "guide"}, today=TODAY)
        altered = deepcopy(KNOWLEDGE)
        altered["articles"][0]["approved"] = "yes"
        with self.assertRaisesRegex(ValueError, "approved"):
            resolve(altered, {"id": "six", "locale": "en", "text": "guide"}, today=TODAY)
        altered = deepcopy(KNOWLEDGE)
        altered["articles"][0]["source"] = "https://"
        with self.assertRaisesRegex(ValueError, "HTTPS URL"):
            resolve(altered, {"id": "six", "locale": "en", "text": "guide"}, today=TODAY)
        altered = deepcopy(SUITE)
        altered["cases"].append(deepcopy(altered["cases"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            evaluate(KNOWLEDGE, altered)


if __name__ == "__main__":
    unittest.main()
