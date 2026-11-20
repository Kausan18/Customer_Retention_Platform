"""Detect an intent category from a customer message."""

import re


_CATEGORY_KEYWORDS = {
    "policy": (
        "refund policy",
        "return policy",
        "exchange policy",
        "how do i",
        "what is the",
        "eligibility",
        "days",
        "window",
        "deadline",
        "policy",
        "rules",
    ),
        "refund": (
        "refund",
        "money back",
        "want my money",
        "get my money",
        "reimbursement",
        "return my order",
        "want to return",
        "i want to return",
        "can i return",
        "how do i return",
        "initiate a return",
        "start a return",
    ),
    "exchange": (
        "exchange",
        "swap",
        "replace",
        "switch",
        "different one",
        "want another",
        "different size",
        "different color",
        "want to exchange",
        "apply for an exchange",
    ),
    "damaged": (
        "damaged",
        "broken",
        "defective",
        "not working",
        "faulty",
        "arrived broken",
        "wrong item",
        "incorrect item",
    ),
    "order_tracking": (
        "where is",
        "track",
        "tracking",
        "shipment",
        "shipped",
        "delivery status",
        "estimated delivery",
        "when will it arrive",
    ),
    "order_status": (
        "order status",
        "payment status",
        "is my payment",
        "order confirmed",
        "order cancelled",
        "what happened to my order",
    ),
    "order_placement": (
        "buy",
        "purchase",
        "want to buy",
        "i want",
        "show me",
        "browse",
        "look at",
        "add to cart",
        "order something",
        "get me",
        "i'd like",
    ),
}

_CATEGORY_PRIORITY = (
    "policy",
    "refund",
    "exchange",
    "damaged",
    "order_tracking",
    "order_status",
    "order_placement",
)

_ORDER_REFERENCE = re.compile(
    r"(?<!\w)ord_[a-z0-9]+(?:_[a-z0-9]+)*(?!\w)",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", text.lower()).split())


def _contains_phrase(message: str, phrase: str) -> bool:
    normalized_phrase = _normalize(phrase)
    return f" {normalized_phrase} " in f" {message} "


def detect_category(message: str) -> str:
    normalized_message = _normalize(message)

    for category in _CATEGORY_PRIORITY:
        if category == "policy" and _ORDER_REFERENCE.search(message):
            continue
        if any(
            _contains_phrase(normalized_message, keyword)
            for keyword in _CATEGORY_KEYWORDS[category]
        ):
            return category

    return "unknown"
