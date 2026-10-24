"""
mock_services/shipping_service.py

Simulates a shipping carrier. Exposes create_shipment and get_tracking.
No financial consequence, so unlike payment_service.py this has no
idempotency store or balance checks — just realistic state variety for
the order_tracking_graph to explain.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

from app.db.database import fetch_one, execute

# --------------------------------------------------------------------------
# Mode control
# --------------------------------------------------------------------------

_VALID_MODES = {"success", "failure", "timeout"}
_mode = "success"


def set_mode(mode: str) -> None:
    """Set the global simulated failure mode. One of: "success" (default),
    "failure", "timeout".
    """
    global _mode
    if mode not in _VALID_MODES:
        raise ValueError(f"Unknown shipping service mode: {mode!r}. Valid: {sorted(_VALID_MODES)}")
    _mode = mode


def get_mode() -> str:
    return _mode


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

def _new_tracking_id() -> str:
    return f"TRK_{uuid.uuid4().hex[:10].upper()}"


def _apply_failure_mode() -> Optional[Dict[str, Any]]:
    if _mode == "success":
        return None

    if _mode == "failure":
        return {
            "success": False,
            "error_code": "CARRIER_UNAVAILABLE",
            "retryable": True,
            "message": "The shipping carrier service is currently unavailable.",
        }

    if _mode == "timeout":
        time.sleep(0.3)
        raise TimeoutError("Shipping provider request timed out.")

    return None


_CARRIERS = ["BlueDart", "Delhivery", "Ekart", "DTDC"]


# --------------------------------------------------------------------------
# Public operations
# --------------------------------------------------------------------------

def create_shipment(order_id: str, carrier: Optional[str], items_summary: str) -> Dict[str, Any]:
    """Create a shipment record for an order. Called after order placement."""
    failure = _apply_failure_mode()
    if failure is not None:
        return failure

    existing = fetch_one("SELECT * FROM shipments WHERE order_id = ?", (order_id,))
    if existing is not None:
        return {
            "success": False,
            "error_code": "SHIPMENT_ALREADY_EXISTS",
            "retryable": False,
            "message": f"Order {order_id} already has a shipment record.",
        }

    tracking_id = _new_tracking_id()
    chosen_carrier = carrier or _CARRIERS[hash(order_id) % len(_CARRIERS)]
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    execute(
        """
        INSERT INTO shipments (id, order_id, carrier, tracking_id, status,
                                estimated_delivery, last_updated)
        VALUES (?, ?, ?, ?, 'processing', NULL, ?)
        """,
        (f"shp_{uuid.uuid4().hex[:10]}", order_id, chosen_carrier, tracking_id, now),
    )

    return {
        "success": True,
        "order_id": order_id,
        "carrier": chosen_carrier,
        "tracking_id": tracking_id,
        "status": "processing",
    }


def get_tracking(order_id: str) -> Dict[str, Any]:
    """Read current shipment state for an order."""
    failure = _apply_failure_mode()
    if failure is not None:
        return failure

    shipment = fetch_one("SELECT * FROM shipments WHERE order_id = ?", (order_id,))
    if shipment is None:
        return {
            "success": False,
            "error_code": "SHIPMENT_NOT_FOUND",
            "retryable": False,
            "message": f"No shipment record found for order {order_id}.",
        }

    return {
        "success": True,
        "order_id": order_id,
        "carrier": shipment["carrier"],
        "tracking_id": shipment["tracking_id"],
        "status": shipment["status"],
        "estimated_delivery": shipment["estimated_delivery"],
        "last_updated": shipment["last_updated"],
    }