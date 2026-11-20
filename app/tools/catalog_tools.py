"""
Catalog tools: product search, exchange proposal creation, exchange execution.

Exchange execution for V1: marks the original order exchange_requested and
creates a new confirmed order for the replacement item. No money moves —
this is a straight swap tracked via order records.
"""
import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from mock_services import payment_service as mock_payment_service
from app.db.database import (
    execute,
    execute_many,
    fetch_all,
    fetch_one,
    get_connection,
)
from app.guardrails.tool_authorizer import check_authorization
from app.services.audit import log_event


CATALOG_STOP_WORDS = frozenset({
    "want", "need", "like", "have", "some", "that", "this", "with", "from",
    "give", "show", "find", "look", "just", "also", "about", "order", "orders",
    "please", "would", "could", "under", "below", "price",
})
STOP_WORDS = frozenset({
    "a", "the", "i", "want", "to", "buy", "for", "me", "please", "some", "an",
})


def extract_catalog_keywords(message: str) -> list[str]:
    words = re.findall(r"[a-zA-Z]{4,}", message.lower())
    return list(dict.fromkeys(word for word in words if word not in CATALOG_STOP_WORDS))


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
    keywords = list(dict.fromkeys(
        word for word in (query or "").lower().split()
        if word not in STOP_WORDS and len(word) > 2
    ))
    if keywords:
        name_matches = ["LOWER(name) LIKE ?" for _ in keywords]
        sql += " AND (" + " OR ".join(name_matches) + ")"
        keyword_patterns = [f"%{word}%" for word in keywords]
        params.extend(keyword_patterns)
        match_score = " + ".join(
            "CASE WHEN LOWER(name) LIKE ? THEN 1 ELSE 0 END"
            for _ in keywords
        )
        sql += f" ORDER BY ({match_score}) DESC, price_minor ASC LIMIT 10"
        params.extend(keyword_patterns)
    else:
        sql += " ORDER BY price_minor ASC LIMIT 10"

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

        # If keyword search returned nothing, try matching query against category name directly
    if not products and query:
        query_lower = query.lower()
        category_rows = fetch_all(
            "SELECT id, name, category, price_minor FROM product_catalog "
            "WHERE in_stock = 1 AND LOWER(category) LIKE ? ORDER BY price_minor ASC LIMIT 10",
            (f"%{query_lower}%",),
        )
        products = [
            {
                "product_id": r["id"],
                "name": r["name"],
                "category": r["category"],
                "price_minor": r["price_minor"],
            }
            for r in category_rows
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

    exchange_id = f"exc_{uuid.uuid4().hex[:8]}"
    original_items = fetch_all(
        "SELECT id FROM order_items WHERE order_id = ?",
        (original_order_id,),
    )
    exchange_statements = [
        (
            """
            INSERT INTO exchanges
                (id, original_order_id, replacement_order_id, customer_id, status, created_at)
            VALUES (?, ?, ?, ?, 'completed', ?)
            """,
            (exchange_id, original_order_id, new_order_id, customer_id, now),
        ),
    ]
    exchange_statements.extend(
        (
            """
            INSERT INTO exchange_items
                (id, exchange_id, original_order_item_id, replacement_product_id, quantity)
            VALUES (?, ?, ?, ?, 1)
            """,
            (
                f"exi_{uuid.uuid4().hex[:8]}",
                exchange_id,
                item["id"],
                product["id"],
            ),
        )
        for item in original_items
    )
    execute_many(exchange_statements)

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

def place_order(
    customer_id: str,
    items: list[dict],
    payment_method: str = "mock_card",
) -> dict:
    """
    Create an order from catalog-priced items and attempt payment.

    Order rows are committed before charging so a failed payment leaves a
    pending order that can be retried.
    """
    if not items:
        return {
            "success": False,
            "error_code": "NO_ITEMS",
            "message": "No items were provided for the order.",
        }

    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    payment_id = f"pay_{uuid.uuid4().hex[:8]}"
    shipment_id = f"shp_{uuid.uuid4().hex[:8]}"
    now = _utcnow()
    resolved_items: list[dict] = []
    total_minor = 0

    try:
        with get_connection() as conn:
            for item in items:
                if not isinstance(item, dict):
                    return {
                        "success": False,
                        "error_code": "INVALID_ITEM",
                        "message": f"Invalid item payload: {item}",
                    }

                product_id = item.get("product_id")
                quantity = item.get("quantity", 1)
                if (
                    not isinstance(product_id, str)
                    or not product_id
                    or not isinstance(quantity, int)
                    or isinstance(quantity, bool)
                    or quantity < 1
                ):
                    return {
                        "success": False,
                        "error_code": "INVALID_ITEM",
                        "message": f"Invalid item payload: {item}",
                    }

                product = conn.execute(
                    "SELECT id, name, category, price_minor, in_stock "
                    "FROM product_catalog WHERE id = ?",
                    (product_id,),
                ).fetchone()

                if product is None:
                    return {
                        "success": False,
                        "error_code": "PRODUCT_NOT_FOUND",
                        "message": f"Product {product_id} not found",
                    }

                if not product["in_stock"]:
                    return {
                        "success": False,
                        "error_code": "OUT_OF_STOCK",
                        "message": f"{product['name']} is out of stock",
                    }

                resolved_items.append({
                    "id": f"oi_{uuid.uuid4().hex[:8]}",
                    "product_id": product["id"],
                    "product_name": product["name"],
                    "category": product["category"],
                    "quantity": quantity,
                    "price_minor": product["price_minor"],
                })
                total_minor += product["price_minor"] * quantity

            conn.execute(
                "INSERT INTO orders (id, customer_id, status, currency, "
                "total_minor, created_at, delivered_at, shipping_address_id) "
                "VALUES (?, ?, 'pending', 'INR', ?, ?, NULL, NULL)",
                (order_id, customer_id, total_minor, now),
            )

            for item in resolved_items:
                conn.execute(
                    "INSERT INTO order_items (id, order_id, product_id, "
                    "product_name, category, quantity, price_minor) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item["id"],
                        order_id,
                        item["product_id"],
                        item["product_name"],
                        item["category"],
                        item["quantity"],
                        item["price_minor"],
                    ),
                )

            conn.execute(
                "INSERT INTO payments (id, order_id, status, amount_minor, "
                "refunded_minor, payment_method, paid_at) "
                "VALUES (?, ?, 'pending', ?, 0, NULL, NULL)",
                (payment_id, order_id, total_minor),
            )

            conn.execute(
                "INSERT INTO shipments (id, order_id, carrier, tracking_id, "
                "status, estimated_delivery, last_updated) "
                "VALUES (?, ?, NULL, NULL, 'processing', NULL, ?)",
                (shipment_id, order_id, now),
            )
    except sqlite3.Error as error:
        return {
            "success": False,
            "error_code": "ORDER_CREATION_FAILED",
            "message": str(error),
        }

    try:
        charge_result = mock_payment_service.charge(
            order_id, total_minor, payment_method
        )
    except (TimeoutError, sqlite3.Error):
        charge_result = {"success": False}

    payment_status = "pending"
    if charge_result.get("success"):
        paid_at = _utcnow()
        provider_reference = (
            charge_result.get("provider_reference")
            or charge_result.get("transaction_id")
        )
        try:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE payments SET status = 'paid', payment_method = ?, "
                    "paid_at = ? WHERE order_id = ?",
                    (payment_method, paid_at, order_id),
                )
                conn.execute(
                    "UPDATE orders SET status = 'confirmed' WHERE id = ?",
                    (order_id,),
                )
                conn.execute(
                    "INSERT INTO payment_transactions "
                    "(id, payment_id, type, amount_minor, idempotency_key, "
                    "provider_reference, status, created_at) "
                    "VALUES (?, ?, 'charge', ?, ?, ?, 'success', ?)",
                    (
                        f"ptx_{uuid.uuid4().hex[:8]}",
                        payment_id,
                        total_minor,
                        f"charge_{order_id}",
                        provider_reference,
                        paid_at,
                    ),
                )
        except sqlite3.Error as error:
            return {
                "success": False,
                "error_code": "PAYMENT_FINALIZATION_FAILED",
                "message": str(error),
            }
        payment_status = "paid"

    return {
        "success": True,
        "order_id": order_id,
        "total_minor": total_minor,
        "items": [
            {
                "product_id": item["product_id"],
                "product_name": item["product_name"],
                "quantity": item["quantity"],
                "price_minor": item["price_minor"],
            }
            for item in resolved_items
        ],
        "payment_status": payment_status,
    }