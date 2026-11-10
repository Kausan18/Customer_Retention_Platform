"""
Catalog tools: product search, exchange proposal creation, exchange execution.

Exchange execution for V1: marks the original order exchange_requested and
creates a new confirmed order for the replacement item. No money moves —
this is a straight swap tracked via order records.
"""
import hashlib
import json
import secrets
from datetime import datetime, timezone, timedelta

from app.db.database import fetch_one, fetch_all, execute
from app.guardrails.tool_authorizer import check_authorization
from app.services.audit import log_event


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def search_catalog(
    category: str = None,
    query: str = None,
    exclude_product_ids: list = None,
) -> dict:
    """
    Find in-stock products, optionally filtered by category and/or query.
    Returns up to 5 results. Used by exchange workflow to find same-category replacements.
    """
    sql = (
        "SELECT id, name, category, price_minor FROM product_catalog WHERE in_stock = 1"
    )
    params = []

    if category:
        sql += " AND category = ?"
        params.append(category)
    if query:
        sql += " AND (LOWER(name) LIKE ? OR LOWER(category) LIKE ?)"
        params.extend([f"%{query.lower()}%", f"%{query.lower()}%"])

    sql += " ORDER BY id LIMIT 10"

    rows = fetch_all(sql, tuple(params)) if params else fetch_all(sql, ())

    products = [
        {
            "product_id": r["id"],
            "name": r["name"],
            "category": r["category"],
            "price_minor": r["price_minor"],
        }
        for r in rows
    ]

    if exclude_product_ids:
        products = [p for p in products if p["product_id"] not in exclude_product_ids]

    # Trim to 5 after exclusion
    products = products[:5]

    return {"success": True, "products": products, "count": len(products)}


def create_exchange_proposal(
    run_id: str,
    original_order_id: str,
    original_items: list,
    replacement_product_id: str,
    replacement_product_name: str,
    replacement_price_minor: int,
    reason: str,
    policy_refs: list,
) -> dict:
    """
    Create an action_proposals row for an exchange. Idempotent.
    """
    existing = fetch_one(
        "SELECT id, payload, status FROM action_proposals "
        "WHERE run_id = ? AND action_type = 'exchange' LIMIT 1",
        (run_id,),
    )
    if existing:
        return {
            "success": True,
            "proposal_id": existing["id"],
            "status": existing["status"],
            "already_existed": True,
        }

    proposal_id = f"prp_{secrets.token_hex(4)}"
    approval_id = f"apr_{secrets.token_hex(4)}"
    now = _utcnow()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    payload = {
        "action_type": "exchange",
        "original_order_id": original_order_id,
        "original_items": original_items,
        "replacement_product_id": replacement_product_id,
        "replacement_product_name": replacement_product_name,
        "replacement_price_minor": replacement_price_minor,
        "reason": reason,
        "policy_refs": policy_refs,
    }
    payload_json = json.dumps(payload, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()

    execute(
        "INSERT INTO action_proposals "
        "(id, run_id, action_type, payload, payload_hash, status, created_at, expires_at) "
        "VALUES (?, ?, 'exchange', ?, ?, 'pending', ?, ?)",
        (proposal_id, run_id, payload_json, payload_hash, now, expires_at),
    )
    execute(
        "INSERT INTO approvals (id, proposal_id, reviewer_id, decision, decided_at, created_at) "
        "VALUES (?, ?, 'pending_reviewer', NULL, NULL, ?)",
        (approval_id, proposal_id, now),
    )

    log_event(
        run_id,
        "system",
        "proposal_created",
        json.dumps({"proposal_id": proposal_id, "action_type": "exchange"}),
    )

    return {
        "success": True,
        "proposal_id": proposal_id,
        "approval_id": approval_id,
        "replacement_product_name": replacement_product_name,
        "expires_at": expires_at,
        "already_existed": False,
    }


def execute_approved_exchange(proposal_id: str, customer_id: str) -> dict:
    """
    Execute an approved exchange: cancel original, create replacement order.
    Gated by tool_authorizer. No money movement — straight swap via order records.
    customer_id used for audit attribution and new order creation.
    """
    auth = check_authorization(proposal_id)
    if not auth.authorized:
        log_event(
            None,
            customer_id,
            "exchange_authorization_failed",
            json.dumps({"proposal_id": proposal_id, "error_code": auth.error_code}),
        )
        return {
            "success": False,
            "error_code": auth.error_code,
            "retryable": False,
            "message": f"Authorization check failed: {auth.error_code}",
        }

    proposal = auth.proposal
    payload = json.loads(proposal["payload"])
    original_order_id = payload["original_order_id"]
    replacement_product_id = payload["replacement_product_id"]
    replacement_price_minor = payload["replacement_price_minor"]
    replacement_name = payload["replacement_product_name"]
    run_id = proposal["run_id"]

    # Mark original order as exchange_requested
    execute(
        "UPDATE orders SET status = 'exchange_requested' WHERE id = ?",
        (original_order_id,),
    )

    # Verify replacement product still exists
    product = fetch_one(
        "SELECT id, name, category FROM product_catalog WHERE id = ? AND in_stock = 1",
        (replacement_product_id,),
    )
    if not product:
        return {
            "success": False,
            "error_code": "PRODUCT_NO_LONGER_AVAILABLE",
            "retryable": False,
            "message": "The replacement product is no longer available.",
        }

    # Create replacement order
    now = _utcnow()
    new_order_id = f"ord_{secrets.token_hex(4)}"
    new_item_id = f"itm_{secrets.token_hex(4)}"
    new_shipment_id = f"shp_{secrets.token_hex(4)}"
    new_payment_id = f"pay_{secrets.token_hex(4)}"
    tracking_id = f"TRK{secrets.token_hex(5).upper()}"
    est_delivery = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()

    execute(
        "INSERT INTO orders (id, customer_id, status, currency, total_minor, created_at) "
        "VALUES (?, ?, 'confirmed', 'INR', ?, ?)",
        (new_order_id, customer_id, replacement_price_minor, now),
    )
    execute(
        "INSERT INTO order_items "
        "(id, order_id, product_id, product_name, category, quantity, price_minor) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (
            new_item_id,
            new_order_id,
            product["id"],
            product["name"],
            product["category"],
            replacement_price_minor,
        ),
    )
    execute(
        "INSERT INTO shipments "
        "(id, order_id, carrier, tracking_id, status, estimated_delivery, last_updated) "
        "VALUES (?, ?, 'BlueDart', ?, 'processing', ?, ?)",
        (new_shipment_id, new_order_id, tracking_id, est_delivery, now),
    )
    execute(
        "INSERT INTO payments "
        "(id, order_id, status, amount_minor, refunded_minor, payment_method, paid_at) "
        "VALUES (?, ?, 'paid', ?, 0, 'exchange', ?)",
        (new_payment_id, new_order_id, replacement_price_minor, now),
    )

    log_event(
        run_id,
        customer_id,
        "exchange_executed",
        json.dumps(
            {
                "proposal_id": proposal_id,
                "original_order_id": original_order_id,
                "new_order_id": new_order_id,
            }
        ),
    )

    return {
        "success": True,
        "original_order_id": original_order_id,
        "new_order_id": new_order_id,
        "replacement_product": replacement_name,
        "tracking_id": tracking_id,
        "status": "exchange_created",
    }