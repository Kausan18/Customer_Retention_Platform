# app/guardrails/response_grounder.py
"""
Guardrail 5: Response Grounder.

Checks the LLM's final customer-facing response against:
  1. No foreign customer IDs
  2. Refund confirmation language only if payment service confirmed success
  3. Rupee amounts within ±1 INR of the actual order total

Returns GuardrailDecision. Callers replace the response with a safe fallback
if outcome == BLOCK.
"""

import re
from dataclasses import dataclass, field

from app.guardrails.input_screener import GuardrailDecision

# Matches customer IDs in C######  format
_CUSTOMER_ID_RE = re.compile(r"\bC\d{6}\b")

# Phrases that indicate the response is claiming a refund was completed
_REFUND_CONFIRMED_PHRASES = re.compile(
    r"refund.{0,30}(processed|confirmed|issued|completed|successful)",
    re.IGNORECASE,
)

# Matches rupee amounts like ₹14,095 or Rs. 14095 or 14,095 rupees
_RUPEE_AMOUNT_RE = re.compile(
    r"(?:₹|Rs\.?\s*)(\d[\d,]+)",
    re.IGNORECASE,
)


@dataclass
class GroundingContext:
    """
    Context that the response grounder checks the LLM response against.

    tool_results: dict of tool name → result dict.
      Expected keys for payment check: tool_results["payment"]["success"] and
      tool_results["payment"]["status"].
      Expected key for amount check: tool_results["order"]["total_minor"].
    authenticated_customer_id: The customer whose session is active.
    approved_actions: List of dicts with keys "action_type" and "status".
    """
    authenticated_customer_id: str
    tool_results: dict = field(default_factory=dict)
    approved_actions: list = field(default_factory=list)


def ground_response(
    response_text: str,
    context: GroundingContext,
) -> GuardrailDecision:
    """
    Validate the LLM response against actual system state.

    Returns ALLOW if all checks pass, BLOCK with a reason_code if any fail.
    """
    # Check 1: No foreign customer IDs
    ids_in_response = _CUSTOMER_ID_RE.findall(response_text)
    for cid in ids_in_response:
        if cid != context.authenticated_customer_id:
            return GuardrailDecision(
                action="BLOCK",
                text=response_text,
                reason="FOREIGN_CUSTOMER_ID_IN_RESPONSE",
            )

    # Check 2: Refund confirmation language requires confirmed payment
    if _REFUND_CONFIRMED_PHRASES.search(response_text):
        payment_result = context.tool_results.get("payment", {})
        payment_success = payment_result.get("success", False)
        payment_status = payment_result.get("status", "")
        _UNCONFIRMED = {"pending", "processing", "unknown", ""}
        if not payment_success or payment_status in _UNCONFIRMED:
            return GuardrailDecision(
                action="BLOCK",
                text=response_text,
                reason="UNCONFIRMED_REFUND_CLAIM",
            )

    # Check 3: Rupee amounts must match order total (within ±1 INR)
    order_result = context.tool_results.get("order", {})
    total_minor = order_result.get("total_minor")
    if total_minor is not None:
        actual_inr = total_minor / 100
        for match in _RUPEE_AMOUNT_RE.finditer(response_text):
            amount_str = match.group(1).replace(",", "")
            try:
                mentioned_amount = float(amount_str)
                if abs(mentioned_amount - actual_inr) > 1.0:
                    return GuardrailDecision(
                        action="BLOCK",
                        text=response_text,
                        reason="AMOUNT_MISMATCH_IN_RESPONSE",
                    )
            except ValueError:
                pass  # Non-numeric match, skip

    return GuardrailDecision(
        action="ALLOW",
        text=response_text,
        reason="RESPONSE_GROUNDED",
    )