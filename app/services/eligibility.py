"""
app/services/eligibility.py

Deterministic, pure-Python eligibility rules for refunds and exchanges.
No LLM involved — this is the financial gate, and it must be reproducible,
auditable, and immune to hallucination (V1_Project_Definition.md Section 6.4:
"check_eligibility <- CODE: deterministic rules, not LLM").

Both public functions return a structured dict rather than a bool or an
exception, so the calling workflow node can branch on `eligible` and
`error_code` without conflating "business rule not met" with "unexpected
error".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

from app.db.database import fetch_one, fetch_all

REFUND_WINDOW_DAYS = 30
EXCHANGE_WINDOW_DAYS = 15

# Return statuses that count as "the return has been received by us",
# i.e. far enough along that a refund can be evaluated.
_RECEIVED_RETURN_STATUSES = {"received", "inspecting", "passed"}

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)

def _parse_dt(value: str) -> datetime:
    """Parse a timestamp stored by seed.py, trying known formats."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {value!r}")


def _ineligible(order_id: str, error_code: str, reason: str) -> Dict[str, Any]:
    return {
        "eligible": False,
        "order_id": order_id,
        "error_code": error_code,
        "reason": reason,
    }


# --------------------------------------------------------------------------
# Refund eligibility
# --------------------------------------------------------------------------

def check_refund_eligibility(order_id: str, now: datetime | None = None) -> Dict[str, Any]:
    """Evaluate whether `order_id` is eligible for a refund right now.

    Conditions, checked in order (first failure short-circuits):
      1. Order exists
      2. Order status is 'delivered'
      3. Within REFUND_WINDOW_DAYS of delivered_at
      4. Payment status is 'paid' or 'partially_refunded' (something left to refund)
      5. Not already fully refunded
      6. A return record exists for this order
      7. Return has been received (status in {received, inspecting, passed})
      8. Return inspection_status == 'passed'

    Returns:
      {"eligible": True, "order_id", "refundable_amount_minor", "reason"}
      or
      {"eligible": False, "order_id", "error_code", "reason"}
    """
    now = now or datetime.now()

    order = fetch_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if order is None:
        return _ineligible(order_id, "ORDER_NOT_FOUND", f"No order found with id {order_id}.")

    if order["status"] != "delivered":
        return _ineligible(
            order_id, "NOT_DELIVERED",
            f"Order status is '{order['status']}'; refunds require delivery first.",
        )

    if not order["delivered_at"]:
        return _ineligible(order_id, "NOT_DELIVERED", "Order has no delivered_at timestamp.")

    delivered_at = _parse_dt(order["delivered_at"])
    window_end = delivered_at + timedelta(days=REFUND_WINDOW_DAYS)
    if now > window_end:
        days_elapsed = (now - delivered_at).days
        return _ineligible(
            order_id, "OUTSIDE_WINDOW",
            f"Order was delivered {days_elapsed} days ago, outside the "
            f"{REFUND_WINDOW_DAYS}-day refund window.",
        )

    payment = fetch_one("SELECT * FROM payments WHERE order_id = ?", (order_id,))
    if payment is None or payment["status"] not in ("paid", "partially_refunded"):
        status = payment["status"] if payment else "no payment record"
        return _ineligible(
            order_id, "PAYMENT_NOT_PAID",
            f"Payment status is '{status}'; refund requires a paid payment.",
        )

    refundable_amount_minor = payment["amount_minor"] - payment["refunded_minor"]
    if refundable_amount_minor <= 0:
        return _ineligible(order_id, "ALREADY_REFUNDED", "This order has already been fully refunded.")

    returns = fetch_all("SELECT * FROM returns WHERE order_id = ? ORDER BY id DESC", (order_id,))
    if not returns:
        return _ineligible(
            order_id, "NO_RETURN_FOUND",
            "No return has been requested for this order yet.",
        )

    latest_return = returns[0]
    if latest_return["status"] not in _RECEIVED_RETURN_STATUSES:
        return _ineligible(
            order_id, "RETURN_NOT_RECEIVED",
            f"Return status is '{latest_return['status']}'; it has not been received yet.",
        )

    if latest_return["inspection_status"] != "passed":
        actual = latest_return["inspection_status"] or "not yet inspected"
        return _ineligible(
            order_id, "INSPECTION_NOT_PASSED",
            f"Return inspection status is '{actual}', not 'passed'.",
        )

    return {
        "eligible": True,
        "order_id": order_id,
        "refundable_amount_minor": refundable_amount_minor,
        "reason": "All refund conditions met: delivered within window, payment paid, "
                  "return received, inspection passed.",
    }


# --------------------------------------------------------------------------
# Exchange eligibility
# --------------------------------------------------------------------------

def check_exchange_eligibility(order_id: str, now: datetime | None = None) -> Dict[str, Any]:
    """Evaluate whether `order_id` is eligible for an exchange right now.

    Conditions, checked in order:
      1. Order exists
      2. Order status is 'delivered'
      3. Within EXCHANGE_WINDOW_DAYS of delivered_at (tighter than refund)
      4. Payment status is 'paid' (not partially/fully refunded)

    Returns:
      {"eligible": True, "order_id", "items": [...], "reason"}
      or
      {"eligible": False, "order_id", "error_code", "reason"}
    """
    now = now or datetime.now()

    order = fetch_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if order is None:
        return _ineligible(order_id, "ORDER_NOT_FOUND", f"No order found with id {order_id}.")

    if order["status"] != "delivered":
        return _ineligible(
            order_id, "NOT_DELIVERED",
            f"Order status is '{order['status']}'; exchanges require delivery first.",
        )

    if not order["delivered_at"]:
        return _ineligible(order_id, "NOT_DELIVERED", "Order has no delivered_at timestamp.")

    delivered_at = _parse_dt(order["delivered_at"])
    window_end = delivered_at + timedelta(days=EXCHANGE_WINDOW_DAYS)
    if now > window_end:
        days_elapsed = (now - delivered_at).days
        return _ineligible(
            order_id, "OUTSIDE_WINDOW",
            f"Order was delivered {days_elapsed} days ago, outside the "
            f"{EXCHANGE_WINDOW_DAYS}-day exchange window.",
        )

    payment = fetch_one("SELECT * FROM payments WHERE order_id = ?", (order_id,))
    if payment is None or payment["status"] != "paid":
        status = payment["status"] if payment else "no payment record"
        return _ineligible(
            order_id, "ALREADY_REFUNDED" if payment and payment["status"] in
            ("refunded", "partially_refunded") else "PAYMENT_NOT_PAID",
            f"Payment status is '{status}'; exchange requires a fully paid, unrefunded order.",
        )

    items = fetch_all(
        "SELECT product_id, product_name, category, quantity, price_minor "
        "FROM order_items WHERE order_id = ?",
        (order_id,),
    )

    return {
        "eligible": True,
        "order_id": order_id,
        "items": items,
        "reason": "All exchange conditions met: delivered within window, payment paid.",
    }