"""
mock_services/payment_service.py

Simulates a third-party payment provider. Exposes charge, refund, and
get_status. In a real system these would be HTTP calls to a provider API;
here they read/write the local `payments` table via app/db/database.py.

Design notes:
- All money in integer minor units (paise). No floats anywhere.
- refund() is idempotent: repeated calls with the same idempotency_key
  return the stored result instead of writing twice. This is V1's
  safety net against duplicate refunds on retry.
- Failure modes are controlled globally via set_mode() so tests and
  eval cases can simulate provider failure, timeout, delay, or a
  provider that hasn't settled yet (pending).
- Structured responses only. No bare exceptions for expected failure
  paths, so callers (tool layer, workflow nodes) can branch on
  error_code / retryable without try/except on business outcomes.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

from app.db.database import fetch_one, execute

# --------------------------------------------------------------------------
# Mode control
# --------------------------------------------------------------------------

_VALID_MODES = {"success", "failure", "timeout", "delay", "pending"}
_mode = "success"

# In-memory idempotency store: idempotency_key -> stored response dict.
# A real provider would persist this; for the mock, process-memory is
# enough since the whole demo runs in one process.
_idempotency_store: Dict[str, Dict[str, Any]] = {}


def set_mode(mode: str) -> None:
    """Set the global simulated failure mode for subsequent calls.

    One of: "success" (default), "failure", "timeout", "delay", "pending".
    """
    global _mode
    if mode not in _VALID_MODES:
        raise ValueError(f"Unknown payment service mode: {mode!r}. Valid: {sorted(_VALID_MODES)}")
    _mode = mode


def get_mode() -> str:
    return _mode


def reset_idempotency_store() -> None:
    """Test helper: clear stored idempotency results between test cases."""
    _idempotency_store.clear()


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

def _new_reference(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _get_payment_row(order_id: str) -> Optional[dict]:
    return fetch_one("SELECT * FROM payments WHERE order_id = ?", (order_id,))


def _apply_failure_mode() -> Optional[Dict[str, Any]]:
    """Apply the current global mode. Returns a structured error dict to
    short-circuit the caller, or None if the caller should proceed normally.
    May raise TimeoutError for "timeout" mode, or sleep for "delay" mode.
    """
    if _mode == "success":
        return None

    if _mode == "failure":
        return {
            "success": False,
            "error_code": "PROVIDER_DECLINED",
            "retryable": False,
            "message": "The payment provider declined this operation.",
        }

    if _mode == "timeout":
        time.sleep(0.3)
        raise TimeoutError("Payment provider request timed out.")

    if _mode == "delay":
        time.sleep(2.5)
        return None  # proceed normally after the delay

    if _mode == "pending":
        return {
            "success": False,
            "error_code": "PROCESSING",
            "retryable": True,
            "status": "pending",
            "message": "The provider has not confirmed this operation yet.",
        }

    return None


# --------------------------------------------------------------------------
# Public operations
# --------------------------------------------------------------------------

def charge(order_id: str, amount_minor: int, payment_method: str) -> Dict[str, Any]:
    """Mark an order as paid. Used during order placement.

    Returns {"success": True, "transaction_id": ...} or a structured failure.
    """
    if amount_minor <= 0:
        return {
            "success": False,
            "error_code": "INVALID_AMOUNT",
            "retryable": False,
            "message": "Charge amount must be positive.",
        }

    failure = _apply_failure_mode()
    if failure is not None:
        return failure

    transaction_id = _new_reference("txn")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    existing = _get_payment_row(order_id)
    if existing is None:
        execute(
            """
            INSERT INTO payments (id, order_id, status, amount_minor, refunded_minor,
                                   payment_method, paid_at)
            VALUES (?, ?, 'paid', ?, 0, ?, ?)
            """,
            (_new_reference("pay"), order_id, amount_minor, payment_method, now),
        )
    else:
        execute(
            """
            UPDATE payments
            SET status = 'paid', amount_minor = ?, payment_method = ?, paid_at = ?
            WHERE order_id = ?
            """,
            (amount_minor, payment_method, now, order_id),
        )

    return {"success": True, "transaction_id": transaction_id, "status": "paid"}


def refund(order_id: str, amount_minor: int, idempotency_key: str) -> Dict[str, Any]:
    """Apply a refund against an existing payment.

    Idempotent on idempotency_key: a second call with the same key returns
    the first call's stored result and performs no additional write. If the
    same key is reused with a *different* amount_minor, that is treated as
    a payload mismatch and rejected (mirrors the PRD's idempotency contract:
    "a changed payload is rejected").
    """
    # --- Idempotency check first, before any failure-mode simulation or
    # balance check, so a confirmed prior result is always returned as-is.
    if idempotency_key in _idempotency_store:
        stored = _idempotency_store[idempotency_key]
        if stored.get("_amount_minor") != amount_minor:
            return {
                "success": False,
                "error_code": "IDEMPOTENCY_PAYLOAD_MISMATCH",
                "retryable": False,
                "message": "This idempotency key was already used with a different amount.",
            }
        # Return the stored result (minus our internal bookkeeping field).
        return {k: v for k, v in stored.items() if k != "_amount_minor"}

    if amount_minor <= 0:
        return {
            "success": False,
            "error_code": "INVALID_AMOUNT",
            "retryable": False,
            "message": "Refund amount must be positive.",
        }

    payment = _get_payment_row(order_id)
    if payment is None:
        return {
            "success": False,
            "error_code": "PAYMENT_NOT_FOUND",
            "retryable": False,
            "message": f"No payment record found for order {order_id}.",
        }

    if payment["status"] != "paid" and payment["status"] != "partially_refunded":
        return {
            "success": False,
            "error_code": "PAYMENT_NOT_REFUNDABLE",
            "retryable": False,
            "message": f"Payment status '{payment['status']}' is not refundable.",
        }

    refundable = payment["amount_minor"] - payment["refunded_minor"]
    if amount_minor > refundable:
        return {
            "success": False,
            "error_code": "AMOUNT_EXCEEDS_BALANCE",
            "retryable": False,
            "message": f"Requested {amount_minor} exceeds refundable balance {refundable}.",
        }

    # Apply the simulated failure mode. Note: a failure/timeout/pending
    # response here is NOT stored in the idempotency map — only a
    # successful, committed operation is. This matches real provider
    # behavior: you can safely retry the same key after a failure.
    failure = _apply_failure_mode()
    if failure is not None:
        return failure

    new_refunded = payment["refunded_minor"] + amount_minor
    new_status = "refunded" if new_refunded >= payment["amount_minor"] else "partially_refunded"

    execute(
        "UPDATE payments SET refunded_minor = ?, status = ? WHERE order_id = ?",
        (new_refunded, new_status, order_id),
    )

    provider_reference = _new_reference("ref")
    result = {
        "success": True,
        "provider_reference": provider_reference,
        "order_id": order_id,
        "amount_minor": amount_minor,
        "new_refunded_minor": new_refunded,
        "status": new_status,
    }

    # Store for idempotent replay, tagged with the amount it was issued for.
    _idempotency_store[idempotency_key] = {**result, "_amount_minor": amount_minor}

    return result


def get_status(order_id: str) -> Dict[str, Any]:
    """Read current payment state for an order."""
    payment = _get_payment_row(order_id)
    if payment is None:
        return {
            "success": False,
            "error_code": "PAYMENT_NOT_FOUND",
            "retryable": False,
            "message": f"No payment record found for order {order_id}.",
        }

    return {
        "success": True,
        "order_id": order_id,
        "status": payment["status"],
        "amount_minor": payment["amount_minor"],
        "refunded_minor": payment["refunded_minor"],
        "payment_method": payment["payment_method"],
        "paid_at": payment["paid_at"],
    }