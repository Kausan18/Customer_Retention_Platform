"""LLM-based customer intent classification."""

import json
import re

from app.services.gemini_client import call_gemini


__all__ = [
    "classify_intent",
    "TRANSACTIONAL_INTENTS",
    "INFORMATIONAL_INTENTS",
]

TRANSACTIONAL_INTENTS = {
    "order_placement",
    "refund",
    "exchange",
    "damaged",
}

INFORMATIONAL_INTENTS = {
    "order_tracking",
    "order_status",
    "policy",
    "greeting",
}

_ALLOWED_INTENTS = TRANSACTIONAL_INTENTS | INFORMATIONAL_INTENTS | {"unknown"}
_FENCED_JSON = re.compile(r"\A```(?:json)?\s*(.*?)\s*```\Z", re.IGNORECASE | re.DOTALL)


def classify_intent(customer_message: str) -> str:
    prompt = f"""SYSTEM:
You are an intent classifier for a customer support system.
Classify the customer message into exactly one of these categories.
Return ONLY valid JSON — no preamble, no explanation, no markdown.

CATEGORIES:
- "order_placement" — customer wants to browse products, buy something, add to cart
- "order_tracking" — customer wants to know where their order is, shipment status
- "order_status" — customer wants to know order/payment status, confirmation
- "refund" — customer wants to request a refund or money back
- "exchange" — customer wants to swap or exchange an item
- "damaged" — customer received a damaged, broken, or wrong item
- "policy" — customer is asking about policies, rules, eligibility, timelines
- "greeting" — hello, hi, how are you, general chat with no support intent
- "unknown" — cannot be classified

RULES:
- "exchange" takes priority over "order_placement" if exchange keywords present
- "refund" takes priority over "order_placement" if refund keywords present
- "policy" is for informational questions only — not for requests that imply an action
- Do NOT classify "i want a refund" as "policy"
- Do NOT classify "i want to exchange" as "order_placement"

OUTPUT FORMAT (respond with ONLY this JSON):
{{"intent": "<category>", "confidence": <0.0-1.0>}}

TASK:
Classify this message: "{customer_message}"
"""
    raw_response = call_gemini(prompt)
    if not isinstance(raw_response, str):
        return "unknown"

    response = raw_response.strip()
    fenced_match = _FENCED_JSON.fullmatch(response)
    if fenced_match:
        response = fenced_match.group(1).strip()

    try:
        result = json.loads(response)
    except json.JSONDecodeError:
        return "unknown"

    if not isinstance(result, dict):
        return "unknown"
    intent = result.get("intent")
    if not isinstance(intent, str) or intent not in _ALLOWED_INTENTS:
        return "unknown"
    return intent