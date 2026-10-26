# app/tools/order_tools.py
"""
Read-only tools for order, shipment, and payment lookups.
All ownership checks happen here, at the data layer — never trust
an order_id supplied by the model or customer without verifying it
belongs to the authenticated customer_id.
"""

from app.db.database import fetch_one, fetch_all


def get_order(order_id: str, customer_id: str) -> dict:
    """
    Fetch a single order, scoped to the authenticated customer.
    Returns the same error for 'does not exist' and 'belongs to someone
    else' — never reveal which case it is.
    """
    row = fetch_one(
        "SELECT id, customer_id, status, currency, total_minor, "
        "created_at, delivered_at FROM orders WHERE id = ? AND customer_id = ?",
        (order_id, customer_id),
    )
    if row is None:
        return {
            "success": False,
            "error_code": "ORDER_NOT_FOUND",
            "retryable": False,
            "message": "No order found for this customer with that ID.",
        }

    items = fetch_all(
        "SELECT product_id, product_name, category, quantity, price_minor "
        "FROM order_items WHERE order_id = ?",
        (order_id,),
    )

    order = dict(row)
    order["items"] = [dict(i) for i in items]

    return {"success": True, "order": order}


def get_recent_orders(customer_id: str, limit: int = 5) -> dict:
    """
    Most recent orders for a customer, newest first. Used when the
    customer's message doesn't name a specific order — we fall back
    to 'their most recent order' rather than guessing.
    """
    rows = fetch_all(
        "SELECT id, status, currency, total_minor, created_at, delivered_at "
        "FROM orders WHERE customer_id = ? ORDER BY created_at DESC LIMIT ?",
        (customer_id, limit),
    )
    orders = [dict(r) for r in rows]

    if not orders:
        return {
            "success": False,
            "error_code": "NO_ORDERS_FOUND",
            "retryable": False,
            "message": "This customer has no orders on record.",
        }

    return {"success": True, "orders": orders}


def get_shipment(order_id: str) -> dict:
    """
    Shipment status for an order. order_id is assumed already
    ownership-verified by a prior get_order() call in the same node —
    this function does not re-check customer_id.
    """
    row = fetch_one(
        "SELECT id, order_id, carrier, tracking_id, status, "
        "estimated_delivery, last_updated FROM shipments WHERE order_id = ?",
        (order_id,),
    )
    if row is None:
        return {
            "success": False,
            "error_code": "SHIPMENT_NOT_FOUND",
            "retryable": False,
            "message": "No shipment record exists for this order.",
        }
    return {"success": True, "shipment": dict(row)}


def get_payment_status(order_id: str) -> dict:
    """
    Payment status for an order. Same ownership assumption as
    get_shipment — caller must have already verified order ownership.
    """
    row = fetch_one(
        "SELECT id, order_id, status, amount_minor, refunded_minor, "
        "payment_method, paid_at FROM payments WHERE order_id = ?",
        (order_id,),
    )
    if row is None:
        return {
            "success": False,
            "error_code": "PAYMENT_NOT_FOUND",
            "retryable": False,
            "message": "No payment record exists for this order.",
        }
    return {"success": True, "payment": dict(row)}