"""Conservative, extractive support answers from approved knowledge only."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

MAX_TEXT = 4096
MAX_ANSWER = 2048
TOKENS = re.compile(r"[^\W_]+", re.UNICODE)
STOP = {"a", "an", "the", "is", "my", "how", "do", "i", "can", "what", "to", "for", "of", "and",
        "في", "من", "على", "ازاي", "كيف", "ما", "هو", "هي", "انا", "عايز", "ممكن", "ال", "عن"}
SENSITIVE = {"refund", "chargeback", "payment", "card", "password", "account", "login", "identity",
             "استرداد", "فلوس", "دفع", "بطاقه", "بطاقة", "كلمه", "كلمة", "مرور", "حساب", "هويه", "هوية", "تسجيل"}


def canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower().replace("ـ", "")
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = value.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
    return value


def terms(value: str) -> set[str]:
    return {token for token in TOKENS.findall(normalize(value)) if len(token) >= 2 and token not in STOP}


def validate_knowledge(snapshot: dict[str, Any], today: date) -> list[dict]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != "1.0":
        raise ValueError("knowledge schema_version must be 1.0")
    articles = snapshot.get("articles")
    if not isinstance(articles, list) or not articles or len(articles) > 1000:
        raise ValueError("knowledge requires 1..1000 articles")
    seen: set[str] = set()
    approved: list[dict] = []
    for article in articles:
        if not isinstance(article, dict) or set(article) != {"id", "locale", "questions", "answer", "source", "approved", "valid_until"}:
            raise ValueError("article fields are invalid")
        article_id = article["id"]
        if not isinstance(article_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", article_id) or article_id in seen:
            raise ValueError("article ID is invalid or duplicated")
        seen.add(article_id)
        if article["locale"] not in {"ar", "en"}:
            raise ValueError("article locale must be ar or en")
        questions = article["questions"]
        if not isinstance(questions, list) or not 1 <= len(questions) <= 20 or any(not isinstance(q, str) or not terms(q) for q in questions):
            raise ValueError("article questions are invalid")
        if not isinstance(article["answer"], str) or not 0 < len(article["answer"]) <= MAX_ANSWER:
            raise ValueError("article answer is invalid")
        source = article["source"]
        parsed = urlsplit(source) if isinstance(source, str) else None
        if (parsed is None or parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError("article source requires an HTTPS URL")
        if type(article["approved"]) is not bool:
            raise ValueError("article approved must be boolean")
        try:
            expiry = date.fromisoformat(article["valid_until"])
        except (TypeError, ValueError):
            raise ValueError("article valid_until must be an ISO date") from None
        if article["approved"] and expiry >= today:
            approved.append(article)
    return approved


def _ticket(ticket: dict) -> tuple[str, str, str]:
    if not isinstance(ticket, dict) or set(ticket) != {"id", "locale", "text"}:
        raise ValueError("ticket must have id, locale and text")
    ticket_id, locale, body = ticket["id"], ticket["locale"], ticket["text"]
    if not isinstance(ticket_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", ticket_id):
        raise ValueError("ticket ID is invalid")
    if locale not in {"ar", "en"} or not isinstance(body, str) or not 0 < len(body) <= MAX_TEXT:
        raise ValueError("ticket locale or text is invalid")
    return ticket_id, locale, body


def resolve(snapshot: dict, ticket: dict, *, today: date | None = None) -> dict:
    """Return an approved answer verbatim or a human escalation; never invoke tools."""
    today = today or datetime.now(UTC).date()
    approved = validate_knowledge(snapshot, today)
    ticket_id, locale, body = _ticket(ticket)
    query = terms(body)
    base = {"schema_version": "1.0", "ticket_id": ticket_id, "locale": locale,
            "knowledge_sha256": canonical_hash(snapshot), "customer_acceptance": "NOT_MEASURED"}
    if not query:
        return {**base, "decision": "escalate", "reason": "insufficient_context", "article_id": None}
    if query & {normalize(token) for token in SENSITIVE}:
        return {**base, "decision": "escalate", "reason": "account_or_payment_action", "article_id": None}
    ranked: list[tuple[float, str, dict]] = []
    for article in approved:
        if article["locale"] != locale:
            continue
        score = max((len(query & terms(question)) / len(query | terms(question))
                     for question in article["questions"]), default=0)
        ranked.append((score, article["id"], article))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked or ranked[0][0] < 0.6:
        return {**base, "decision": "escalate", "reason": "no_grounded_answer", "article_id": None}
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.15:
        return {**base, "decision": "escalate", "reason": "ambiguous_knowledge", "article_id": None}
    article = ranked[0][2]
    return {**base, "decision": "answer", "reason": "approved_knowledge", "article_id": article["id"],
            "answer": article["answer"], "source": article["source"],
            "claim_boundary": "Approved article returned verbatim; issue resolution and customer acceptance are unverified."}
