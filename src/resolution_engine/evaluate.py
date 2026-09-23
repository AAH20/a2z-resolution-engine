"""Recorded-case evaluation with explicit modeled unit economics."""

from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .engine import canonical_hash, resolve


def _money(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError(f"{name} must be a nonnegative finite number")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{name} must be a nonnegative finite number") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be a nonnegative finite number")
    return result


def evaluate(snapshot: dict, suite: dict) -> dict:
    if not isinstance(suite, dict) or suite.get("schema_version") != "1.0":
        raise ValueError("suite schema_version must be 1.0")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > 10000:
        raise ValueError("suite requires 1..10000 cases")
    cost = suite.get("modeled_costs_usd")
    if not isinstance(cost, dict):
        raise TypeError("modeled_costs_usd is required")
    per_case = _money(cost.get("per_case"), "per_case")
    per_escalation = _money(cost.get("per_escalation"), "per_escalation")
    run_date = date.fromisoformat(suite["as_of"])
    ids: set[str] = set()
    correct = accepted = answered = escalated = 0
    for item in cases:
        if not isinstance(item, dict) or set(item) != {"ticket", "expected_decision", "expected_article_id", "declared_accepted"}:
            raise ValueError("case fields are invalid")
        ticket = item["ticket"]
        result = resolve(snapshot, ticket, today=run_date)
        if result["ticket_id"] in ids:
            raise ValueError("duplicate case ID")
        ids.add(result["ticket_id"])
        if item["expected_decision"] not in {"answer", "escalate"} or type(item["declared_accepted"]) is not bool:
            raise ValueError("case expectation is invalid")
        if result["decision"] == item["expected_decision"] and result["article_id"] == item["expected_article_id"]:
            correct += 1
        if result["decision"] == "answer":
            answered += 1
            accepted += int(item["declared_accepted"] and result["article_id"] == item["expected_article_id"])
        else:
            escalated += 1
    total = per_case * len(cases) + per_escalation * escalated
    return {
        "schema_version": "1.0", "suite_sha256": canonical_hash(suite),
        "knowledge_sha256": canonical_hash(snapshot), "cases": len(cases),
        "correct_against_declared_labels": correct, "answered": answered,
        "escalated": escalated, "declared_accepted_answers": accepted,
        "accuracy_against_declared_labels": round(correct / len(cases), 4),
        "modeled_total_cost_usd": str(total.quantize(Decimal("0.01"))),
        "modeled_cost_per_declared_accepted_answer_usd": str((total / accepted).quantize(Decimal("0.0001"))) if accepted else None,
        "claim_boundary": "Labels, acceptance and costs are supplied by the suite. No customer resolution, cost saving or live performance is established.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a recorded support case suite")
    parser.add_argument("knowledge", type=Path)
    parser.add_argument("suite", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(json.loads(args.knowledge.read_text()), json.loads(args.suite.read_text()))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
