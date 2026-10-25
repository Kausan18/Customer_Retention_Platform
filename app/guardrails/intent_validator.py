# app/guardrails/intent_validator.py
"""
Guardrail 2: Intent Validator.

Called after the input screener and after the LLM classifier returns an intent.
For Checkpoint 3, also called directly with the ticket category as a proxy intent
(since there is no LLM yet).

Returns a GuardrailDecision. Callers check decision.outcome before dispatching
to a LangGraph graph.
"""

from app.guardrails.input_screener import GuardrailDecision

SUPPORTED_INTENTS = frozenset({
    "order_placement",
    "order_tracking",
    "order_status",
    "refund",
    "exchange",
})

# Keywords that clearly indicate an out-of-scope request.
# Not exhaustive — the LLM classifier handles edge cases in Week 2.
_OUT_OF_SCOPE_KEYWORDS = frozenset({
    "weather", "joke", "jokes", "recipe", "sports", "news",
    "politics", "competitor", "stock price", "movie", "song",
    "homework", "translate", "poem",
})


def validate_intent(
    raw_intent: str | None,
    customer_message: str = "",
) -> GuardrailDecision:
    """
    Validate that raw_intent is one of the 5 supported workflow intents.

    Args:
        raw_intent: The intent string returned by the classifier (or ticket category).
        customer_message: The original customer message. Used for keyword pre-check.

    Returns:
        GuardrailDecision with outcome ALLOW or BLOCK.
    """
    # Guard: missing intent
    if not raw_intent or not raw_intent.strip():
        return GuardrailDecision(
            action="BLOCK",
            text=customer_message,
            reason="INTENT_UNCLEAR",
        )

    normalized = raw_intent.strip().lower()

    # Guard: out-of-scope by keyword match on customer message
    if normalized not in SUPPORTED_INTENTS:
        msg_lower = customer_message.lower()
        for keyword in _OUT_OF_SCOPE_KEYWORDS:
            if keyword in msg_lower:
                return GuardrailDecision(
                    action="BLOCK",
                    text=customer_message,
                    reason="INTENT_OUT_OF_SCOPE",
                )

    # Guard: intent not in supported set
    if normalized not in SUPPORTED_INTENTS:
        return GuardrailDecision(
            action="BLOCK",
            text=customer_message,
            reason="INTENT_OUT_OF_SCOPE",
        )

    return GuardrailDecision(
        action="ALLOW",
        text=normalized,
        reason="INTENT_VALID",
    )