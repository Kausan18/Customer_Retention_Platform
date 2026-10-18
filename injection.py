from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

MAX_INPUT_CHARS = 500

INJECTION_PATTERNS = (
    r"\b(ignore|disregard|forget|override)\b.{0,60}\b(instructions?|rules?|system prompt|developer message)\b",
    r"\b(reveal|show|print|repeat|dump|export|leak)\b.{0,60}\b(system prompt|developer message|hidden instructions?)\b",
    r"\b(bypass|disable|ignore|turn off)\b.{0,40}\b(guardrails?|safety|security|restrictions?)\b",
    r"\b(jailbreak|dan mode|act as|pretend to be)\b",
    r"\b(dump|export|list|show|reveal|print)\b.{0,60}\b(all|other|every|another)\s+(customers?|customer records|accounts?|account records|database)\b",
    r"\b(all|other|every|another)\s+(customers?|customer records|accounts?|account records)\b",
    r"\b(full|complete|entire)\s+(pan|account number|card number)\b",
    r"\b(pan|account number|card number)\b",
    r"\binternal[_ ]risk[_ ]score\b",
)


@dataclass(frozen=True)
class GuardrailDecision:
    action: Literal["ALLOW", "SANITIZE", "BLOCK"]
    text: str
    reason: str


def inspect_request(text: str, authenticated_customer_id: str) -> GuardrailDecision:
    """Normalize user input and block injection or out-of-scope banking requests."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith("C")
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()

    if not normalized:
        return GuardrailDecision("BLOCK", "", "empty_input")
    if len(text) > MAX_INPUT_CHARS:
        return GuardrailDecision("BLOCK", "", "input_too_long")

    lowered = normalized.casefold()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            return GuardrailDecision("BLOCK", "", "suspected_prompt_injection")

    customer_ids = re.findall(r"\bCUST_\d{3}\b", normalized.upper())
    if any(customer_id != authenticated_customer_id for customer_id in customer_ids):
        return GuardrailDecision("BLOCK", "", "cross_customer_access")

    if normalized != text:
        return GuardrailDecision("SANITIZE", normalized, "input_normalized")
    return GuardrailDecision("ALLOW", normalized, "input_accepted")


def inspect_response(text: str) -> bool:
    """Return whether a model response appears to contain a protected field."""
    sensitive_patterns = (
        r"\bMOCKPAN\d+X\b",
        r"\b\d{10,}\b",
        r"\binternal[_ ]risk[_ ]score\b",
        r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
    )
    return any(
        re.search(pattern, text, flags=re.IGNORECASE)
        for pattern in sensitive_patterns
    )
