# app/tools/shipping_tools.py
"""
Wrapper around the mock shipping service. Kept separate from
order_tools.py so that swapping the mock for a real carrier API
in V2 only touches this file.
"""

from mock_services.shipping_service import get_tracking


def get_tracking_info(order_id: str) -> dict:
    """
    Normalizes the shipping service's response to the standard
    tool contract. The underlying service already returns structured
    data (see mock_services/shipping_service.py) — this just ensures
    a consistent 'success' key regardless of which failure mode is active.
    """
    result = get_tracking(order_id)

    if not result or not result.get("success", False):
        return {
            "success": False,
            "error_code": result.get("error_code", "CARRIER_UNAVAILABLE") if result else "CARRIER_UNAVAILABLE",
            "retryable": result.get("retryable", True) if result else True,
            "message": result.get("message", "Shipping carrier is unavailable.") if result else "Shipping carrier is unavailable.",
        }

    return {
        "success": True,
        "tracking": {
            "carrier": result.get("carrier"),
            "tracking_id": result.get("tracking_id"),
            "status": result.get("status"),
            "estimated_delivery": result.get("estimated_delivery"),
            "last_updated": result.get("last_updated"),
        },
    }