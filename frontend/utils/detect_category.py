"""Detect an intent category from a customer message."""

import re


_CATEGORY_KEYWORDS = {
    "policy": (
        "policy", "policies", "how does", "how do i", "what is the",
        "what are the", "can i return", "can i exchange", "am i eligible",
        "am i entitled", "what happens if", "how long does", "rules", "terms",
        "conditions", "eligible", "eligibility", "deadline", "window",
        "days to return", "shipping policy", "refund policy", "return policy",
        "exchange policy", "damaged item policy", "how are refunds",
        "when will i get", "terms and conditions", "return window",
        "refund window", "exchange window", "delivery policy",
        "how do returns work", "what are my rights", "refund timeline",
    ),
    "refund": (
        "refund", "refunds", "money back", "get my money", "reimburse",
        "reimbursement", "want my money back", "return my money", "charge back",
        "paid but", "not received refund", "where is my refund", "refund status",
        "refund request", "request refund", "process refund", "refunded",
        "refundable", "get a refund", "refund me", "refund my order",
        "return", "return item", "return an item", "return the item",
        "return my order", "return this", "return product", "send back",
        "send it back", "money returned", "credited back", "chargeback",
        "cancel and refund", "cash back", "refund pending",
        "still waiting for refund", "refund has not arrived",
        "refund hasn t arrived",
    ),
    "exchange": (
        "exchange", "swap", "replace", "replacement", "switch",
        "different size", "different colour", "different color", "wrong size",
        "wrong item", "want a different", "trade", "change my order",
        "exchange request", "want to swap", "switch product", "exchanged",
        "exchanging", "exchange this", "exchange it", "replace it",
        "get replacement", "replacement item", "swap this", "switch to",
        "wrong product", "incorrect item", "change size", "change colour",
        "change color", "different product", "another size",
    ),
    "order_tracking": (
        "where is my order", "where is my package", "track", "tracking",
        "shipment", "shipped", "dispatch", "dispatched", "courier",
        "delivery status", "out for delivery", "in transit",
        "when will it arrive", "has it shipped", "has my order shipped",
        "delivery update", "estimated delivery", "expected delivery",
        "track my order", "track my package", "tracking number",
        "delivery tracking", "shipment tracking", "dispatch status",
        "arriving today", "when is it arriving", "where is the package",
        "package hasn t arrived", "has my package shipped", "delivery date",
        "delayed shipment", "courier status",
    ),
    "order_status": (
        "order status", "status of my order", "check my order",
        "order update", "what is happening with", "what happened to my order",
        "is my order", "order confirmed", "order cancelled", "payment status",
        "did my order go through", "payment confirmed", "was my order placed",
        "check order status", "status of order", "my order status",
        "order placed", "confirm my order", "is order confirmed",
        "did you receive my order", "payment went through", "payment failed",
        "order pending", "order processing", "order history",
    ),
    "order_placement": (
        "buy", "purchase", "order", "want to order", "place an order",
        "i want", "i need", "get me", "show me", "browse", "look at",
        "have a look", "can i see", "what do you have", "what do you sell",
        "available", "in stock", "do you have", "looking for",
        "searching for", "find me", "recommend", "suggest", "add to cart",
        "checkout", "cart", "shop", "shopping", "face cream", "headphones",
        "laptop", "shoes", "toy", "shirt", "electronics", "clothing",
        "cosmetics", "food", "toys", "sneakers", "boots", "moisturizer",
        "serum", "jacket", "watch", "i d like to order", "place order",
        "buy this", "purchase this", "shop for", "browse products",
        "available products", "show products", "add product",
    ),
}

_CATEGORY_PRIORITY = (
    "policy",
    "refund",
    "exchange",
    "order_tracking",
    "order_status",
    "order_placement",
)

_CONFIRMATIONS = {
    "yes", "yes please", "yeah", "yep", "yup", "sure", "okay", "ok",
    "confirm", "confirmed", "that one", "the one", "this one",
    "first option", "second option", "third option", "option one",
    "option two", "option three", "i ll take it", "ill take it",
    "i will take it", "i ll take that", "ill take that", "sounds good",
    "go ahead", "do that", "looks good", "that s right", "thats right",
    "please do", "the first one", "the second one", "the third one",
}


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", text.lower()).split())


def _contains_phrase(message: str, phrase: str) -> bool:
    normalized_phrase = _normalize(phrase)
    return f" {normalized_phrase} " in f" {message} "


def detect_category(message: str) -> str:
    normalized_message = _normalize(message)

    for category in _CATEGORY_PRIORITY:
        keywords = _CATEGORY_KEYWORDS[category]
        if (
            category == "policy"
            and _contains_phrase(normalized_message, "status of my order")
        ):
            # A generic policy opener must not mask an explicit order-status intent.
            keywords = tuple(keyword for keyword in keywords if keyword != "what is the")
        if any(_contains_phrase(normalized_message, keyword) for keyword in keywords):
            return category

    if normalized_message in _CONFIRMATIONS or re.fullmatch(
        r"(?:option|choice|number)\s+\d+", normalized_message
    ):
        return "order_placement"

    return "order_placement"
