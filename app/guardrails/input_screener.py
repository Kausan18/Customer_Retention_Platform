from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

MAX_INPUT_CHARS = 500
MAX_RESPONSE_CHARS = 2000

INJECTION_PATTERNS = (
    r"\b(ignore|disregard|forget|override)\b.{0,60}\b(instructions?|rules?|system prompt|developer message)\b",
    r"\b(reveal|show|print|repeat|dump|export|leak)\b.{0,60}\b(system prompt|developer message|hidden instructions?)\b",
    r"\b(bypass|disable|ignore|turn off)\b.{0,40}\b(guardrails?|safety|security|restrictions?)\b",
    r"\b(jailbreak|dan mode|act as|pretend to be)\b",
    r"\b(dump|export|list|show|reveal|print|query)\b.{0,60}\b(all|other|every|another)\s+(customers?|customer records|accounts?|account records|users?|user records|database|purchases)\b",
    r"\b(all|other|every|another)\s+(customers?|customer records|accounts?|account records|users?|user records)\b",
    r"\b(full|complete|entire)\s+(pan|account number|card number)\b",
    r"\b(pan|account number|card number|phone number|email address)\b",
    r"\binternal[_ ]risk[_ ]score\b",
    r"\b(sqlite_master|information_schema|select\s+.+\s+from|drop\s+table|union\s+select)\b",
    r"\b(database|table)\s+(schema|structure|contents?)\b",
    r"\b(what is|what's|show me|tell me|display)\b.{0,40}\b(system prompt|developer message|hidden instructions?)\b",
    r"\b(run|call|invoke|execute|trigger)\b.{0,40}\b(execute_|create_|get_|update_|search_)\w+",
)

SUPPORTED_TOPICS = (
    r"\b(purchases?|bought|buy|spend|spent|spending|total|average|amount|orders?|transactions?|invoices?)\b",
    r"\b(categories|category|products?|items?)\b",
    r"\b(payment|payments|paid|pay|cash|credit|debit)\b",
    r"\b(malls?|stores?|shopping|shops?)\b",
)


@dataclass(frozen=True)
class GuardrailDecision:
    action: Literal["ALLOW", "SANITIZE", "BLOCK"]
    text: str
    reason: str


def inspect_request(text: str, authenticated_customer_id: str) -> GuardrailDecision:
    """Normalize input and block injection or cross-customer access attempts."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(
        " "
        if unicodedata.category(character).startswith("C") and character.isspace()
        else character
        for character in normalized
        if not unicodedata.category(character).startswith("C") or character.isspace()
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

    customer_ids = re.findall(
        r"\b(?:CUST_\d{3}|C\d{6})\b", normalized.upper()
    )
    if any(
        customer_id != authenticated_customer_id.upper()
        for customer_id in customer_ids
    ):
        return GuardrailDecision("BLOCK", "", "cross_customer_access")

    if normalized != text:
        return GuardrailDecision("SANITIZE", normalized, "input_normalized")
    return GuardrailDecision("ALLOW", normalized, "input_accepted")


def is_supported_question(text: str) -> bool:
    """Restrict chatbot questions to topics available in the shopping dataset."""
    return any(
        re.search(pattern, text, flags=re.IGNORECASE)
        for pattern in SUPPORTED_TOPICS
    )


def clean_response(text: str) -> str:
    """Remove invisible controls and Markdown decoration from model output."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith("C")
        or character in "\r\n\t"
    )
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in normalized.split("\n"):
        line = re.sub(r"[^\S\n]+", " ", line).strip()
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        line = re.sub(r"^\s*>\s?", "", line)
        line = re.sub(r"(\*\*|__)(.*?)\1", r"\2", line)
        line = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", line)
        line = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", line)
        line = line.replace("`", "")
        lines.append(line)
    cleaned_lines: list[str] = []
    for line in lines:
        if line or (cleaned_lines and cleaned_lines[-1]):
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def inspect_response(text: str) -> bool:
    """Return whether a model response appears to contain protected identifiers."""
    if not text or len(text) > MAX_RESPONSE_CHARS:
        return True
    sensitive_patterns = (
        r"\bMOCKPAN\d+X\b",
        r"\b\d{10,}\b",
        r"\b(?:CUST_\d{3}|C\d{6}|I\d{6})\b",
        r"\binternal[_ ]risk[_ ]score\b",
        r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
        r"\b(system prompt|developer message|authorized customer purchase summary)\b",
    )
    return any(
        re.search(pattern, text, flags=re.IGNORECASE)
        for pattern in sensitive_patterns
    )
