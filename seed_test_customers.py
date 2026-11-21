"""
scripts/seed_test_customers.py

Appends 10 test customers with predictable order states for workflow testing.
Safe to run multiple times — skips rows that already exist by primary key.

Run from project root:
    python scripts/seed_test_customers.py

Test credentials (all customers):  PIN = 1234
Reviewer credentials:               R000001 / R000002  PIN = 9999
"""

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path("data/customer_support.sqlite3")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def days_from_now(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=n)).isoformat()


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Test customer definitions
# ---------------------------------------------------------------------------

CUSTOMERS = [
    {
        "customer_id": "C000001",
        "name":        "Aanya Sharma",
        "email":       "aanya@test.com",
        "scenario":    "eligible_refund",
    },
    {
        "customer_id": "C000002",
        "name":        "Bikram Das",
        "email":       "bikram@test.com",
        "scenario":    "ineligible_refund_past_window",
    },
    {
        "customer_id": "C000003",
        "name":        "Charu Nair",
        "email":       "charu@test.com",
        "scenario":    "eligible_exchange",
    },
    {
        "customer_id": "C000004",
        "name":        "Deepak Pillai",
        "email":       "deepak@test.com",
        "scenario":    "ineligible_exchange_past_window",
    },
    {
        "customer_id": "C000005",
        "name":        "Esha Kulkarni",
        "email":       "esha@test.com",
        "scenario":    "eligible_return_in_progress",
    },
    {
        "customer_id": "C000006",
        "name":        "Farhan Qureshi",
        "email":       "farhan@test.com",
        "scenario":    "ineligible_return_past_window",
    },
    {
        "customer_id": "C000007",
        "name":        "Geetika Rao",
        "email":       "geetika@test.com",
        "scenario":    "eligible_damaged_within_window",
    },
    {
        "customer_id": "C000008",
        "name":        "Harish Menon",
        "email":       "harish@test.com",
        "scenario":    "ineligible_damaged_past_window",
    },
    {
        "customer_id": "C000009",
        "name":        "Isha Verma",
        "email":       "isha@test.com",
        "scenario":    "shipping_delay",
    },
    {
        "customer_id": "C000010",
        "name":        "Jayant Bose",
        "email":       "jayant@test.com",
        "scenario":    "clean_delivered_no_issues",
    },
]

# One product per scenario category so test cases are unambiguous
PRODUCTS = {
    "electronics": ("prod_test_e1", "Sony WH-1000XM5 Headphones", "Electronics", 2999900),
    "shoes":       ("prod_test_s1", "Nike Air Max 270",            "Shoes",       899900),
    "clothing":    ("prod_test_c1", "Levi's 511 Slim Fit Jeans",   "Clothing",    399900),
    "toys":        ("prod_test_t1", "LEGO Classic Creative Bricks", "Toys",       149900),
}

ADDRESS = {
    "line1":   "42 MG Road",
    "line2":   "Apt 5B",
    "city":    "Bengaluru",
    "state":   "Karnataka",
    "pincode": "560001",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def skip_if_exists(conn: sqlite3.Connection, table: str, pk_col: str, pk_val: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE {pk_col} = ?", (pk_val,)
    ).fetchone()
    return row is not None


def insert_user(conn, customer_id: str, name: str, email: str) -> str:
    user_id = f"usr_{customer_id.lower()}"
    if not skip_if_exists(conn, "users", "customer_id", customer_id):
        conn.execute(
            "INSERT INTO users (id, customer_id, name, email, pin, role, created_at) "
            "VALUES (?, ?, ?, ?, '1234', 'customer', ?)",
            (user_id, customer_id, name, email, utcnow()),
        )
        print(f"  + user {customer_id} ({name})")
    else:
        print(f"  ~ user {customer_id} already exists, skipping")
    return user_id



def insert_product(conn, product_id, name, category, price_minor):
    if not skip_if_exists(conn, "product_catalog", "id", product_id):
        conn.execute(
            "INSERT INTO product_catalog (id, name, category, price_minor, in_stock) "
            "VALUES (?, ?, ?, ?, 1)",
            (product_id, name, category, price_minor),
        )
        print(f"  + product {product_id} ({name})")


def insert_order(conn, customer_id, product_id, product_name,
                 category, price_minor, order_status,
                 created_days_ago, delivered_days_ago=None) -> tuple[str, str]:
    order_id = f"ord_tc_{customer_id.lower()}"
    item_id  = f"oi_tc_{customer_id.lower()}"

    if skip_if_exists(conn, "orders", "id", order_id):
        print(f"  ~ order {order_id} already exists, skipping")
        return order_id, item_id

    delivered_at = days_ago(delivered_days_ago) if delivered_days_ago else None

    conn.execute(
        "INSERT INTO orders (id, customer_id, status, currency, total_minor, "
        "created_at, delivered_at, shipping_address_id) "
        "VALUES (?, ?, ?, 'INR', ?, ?, ?, NULL)",
        (order_id, customer_id, order_status, price_minor,
         days_ago(created_days_ago), delivered_at),
    )
    conn.execute(
        "INSERT INTO order_items (id, order_id, product_id, product_name, "
        "category, quantity, price_minor) VALUES (?, ?, ?, ?, ?, 1, ?)",
        (item_id, order_id, product_id, product_name, category, price_minor),
    )
    print(f"  + order {order_id} status={order_status} delivered_days_ago={delivered_days_ago}")
    return order_id, item_id


def insert_shipment(conn, order_id, ship_status, estimated_delivery_days_from_now=None,
                    estimated_delivery_days_ago=None):
    ship_id = f"shp_tc_{order_id.split('_')[-1]}"
    if skip_if_exists(conn, "shipments", "id", ship_id):
        return
    if estimated_delivery_days_from_now is not None:
        est = days_from_now(estimated_delivery_days_from_now)
    elif estimated_delivery_days_ago is not None:
        est = days_ago(estimated_delivery_days_ago)
    else:
        est = None
    conn.execute(
        "INSERT INTO shipments (id, order_id, carrier, tracking_id, status, "
        "estimated_delivery, last_updated) VALUES (?, ?, 'BlueDart', ?, ?, ?, ?)",
        (ship_id, order_id, f"TRK{order_id[-6:].upper()}", ship_status, est, utcnow()),
    )
    print(f"  + shipment {ship_id} status={ship_status}")


def insert_payment(conn, order_id, price_minor, pay_status="paid"):
    pay_id = f"pay_tc_{order_id.split('_')[-1]}"
    if skip_if_exists(conn, "payments", "id", pay_id):
        return
    paid_at = utcnow() if pay_status == "paid" else None
    conn.execute(
        "INSERT INTO payments (id, order_id, status, amount_minor, refunded_minor, "
        "payment_method, paid_at) VALUES (?, ?, ?, ?, 0, 'upi', ?)",
        (pay_id, order_id, pay_status, price_minor, paid_at),
    )
    print(f"  + payment {pay_id} status={pay_status}")


def insert_return(conn, order_id, return_status, inspection_status=None,
                  received_days_ago=None):
    ret_id = f"ret_tc_{order_id.split('_')[-1]}"
    if skip_if_exists(conn, "returns", "id", ret_id):
        return
    received_at = days_ago(received_days_ago) if received_days_ago else None
    conn.execute(
        "INSERT INTO returns (id, order_id, status, reason, received_at, inspection_status) "
        "VALUES (?, ?, ?, 'Customer requested return', ?, ?)",
        (ret_id, order_id, return_status, received_at, inspection_status),
    )
    print(f"  + return {ret_id} status={return_status} inspection={inspection_status}")


# ---------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------

def seed_eligible_refund(conn, c):
    pid, pname, pcat, pprice = PRODUCTS["electronics"]
    insert_user(conn, c["customer_id"], c["name"], c["email"])
    insert_product(conn, pid, pname, pcat, pprice)
    order_id, _ = insert_order(conn, c["customer_id"], pid, pname, pcat,
                                pprice, "delivered",
                                created_days_ago=12, delivered_days_ago=5)
    insert_shipment(conn, order_id, "delivered")
    insert_payment(conn, order_id, pprice, "paid")
    insert_return(conn, order_id, "passed", "passed", received_days_ago=3)
    print(f"  → eligible for REFUND of ₹{pprice/100:.2f}")


def seed_ineligible_refund(conn, c):
    """Delivered 45 days ago — outside the 30-day refund window."""
    pid, pname, pcat, pprice = PRODUCTS["electronics"]
    user_id = insert_user(conn, c["customer_id"], c["name"], c["email"])
    insert_product(conn, pid, pname, pcat, pprice)
    order_id, _ = insert_order(conn, c["customer_id"], pid, pname, pcat,
                                pprice, "delivered",
                                created_days_ago=50, delivered_days_ago=45)
    insert_shipment(conn, order_id, "delivered")
    insert_payment(conn, order_id, pprice, "paid")
    insert_return(conn, order_id, "passed", "passed", received_days_ago=40)
    print(f"  → INELIGIBLE for refund — 45 days since delivery (window: 30 days)")


def seed_eligible_exchange(conn, c):
    """Delivered 5 days ago — within the 15-day exchange window."""
    pid, pname, pcat, pprice = PRODUCTS["shoes"]
    user_id = insert_user(conn, c["customer_id"], c["name"], c["email"])
    insert_product(conn, pid, pname, pcat, pprice)
    order_id, _ = insert_order(conn, c["customer_id"], pid, pname, pcat,
                                pprice, "delivered",
                                created_days_ago=10, delivered_days_ago=5)
    insert_shipment(conn, order_id, "delivered")
    insert_payment(conn, order_id, pprice, "paid")
    print(f"  → eligible for EXCHANGE (delivered 5 days ago, window: 15 days)")


def seed_ineligible_exchange(conn, c):
    """Delivered 20 days ago — outside the 15-day exchange window."""
    pid, pname, pcat, pprice = PRODUCTS["shoes"]
    user_id = insert_user(conn, c["customer_id"], c["name"], c["email"])
    insert_product(conn, pid, pname, pcat, pprice)
    order_id, _ = insert_order(conn, c["customer_id"],pid, pname, pcat,
                                pprice, "delivered",
                                created_days_ago=25, delivered_days_ago=20)
    insert_shipment(conn, order_id, "delivered")
    insert_payment(conn, order_id, pprice, "paid")
    print(f"  → INELIGIBLE for exchange — 20 days since delivery (window: 15 days)")


def seed_eligible_return(conn, c):
    """Delivered 3 days ago. Return requested but inspection not yet complete."""
    pid, pname, pcat, pprice = PRODUCTS["clothing"]
    user_id = insert_user(conn, c["customer_id"], c["name"], c["email"])
    insert_product(conn, pid, pname, pcat, pprice)
    order_id, _ = insert_order(conn, c["customer_id"], pid, pname, pcat,
                                pprice, "delivered",
                                created_days_ago=8, delivered_days_ago=3)
    insert_shipment(conn, order_id, "delivered")
    insert_payment(conn, order_id, pprice, "paid")
    insert_return(conn, order_id, "received", None, received_days_ago=1)
    print(f"  → return IN PROGRESS — received, awaiting inspection")


def seed_ineligible_return(conn, c):
    """Delivered 45 days ago — outside any return window."""
    pid, pname, pcat, pprice = PRODUCTS["clothing"]
    user_id = insert_user(conn, c["customer_id"], c["name"], c["email"])
    insert_product(conn, pid, pname, pcat, pprice)
    order_id, _ = insert_order(conn, c["customer_id"],pid, pname, pcat,
                                pprice, "delivered",
                                created_days_ago=50, delivered_days_ago=45)
    insert_shipment(conn, order_id, "delivered")
    insert_payment(conn, order_id, pprice, "paid")
    print(f"  → INELIGIBLE for return — 45 days since delivery")


def seed_eligible_damaged(conn, c):
    """Delivered 2 days ago — within the 7-day damage report window."""
    pid, pname, pcat, pprice = PRODUCTS["toys"]
    user_id = insert_user(conn, c["customer_id"], c["name"], c["email"])
    insert_product(conn, pid, pname, pcat, pprice)
    order_id, _ = insert_order(conn, c["customer_id"], pid, pname, pcat,
                                pprice, "delivered",
                                created_days_ago=7, delivered_days_ago=2)
    insert_shipment(conn, order_id, "delivered")
    insert_payment(conn, order_id, pprice, "paid")
    print(f"  → eligible for DAMAGED ITEM report (delivered 2 days ago, window: 7 days)")


def seed_ineligible_damaged(conn, c):
    """Delivered 15 days ago — outside the 7-day damage report window."""
    pid, pname, pcat, pprice = PRODUCTS["toys"]
    user_id = insert_user(conn, c["customer_id"], c["name"], c["email"])
    insert_product(conn, pid, pname, pcat, pprice)
    order_id, _ = insert_order(conn, c["customer_id"], pid, pname, pcat,
                                pprice, "delivered",
                                created_days_ago=20, delivered_days_ago=15)
    insert_shipment(conn, order_id, "delivered")
    insert_payment(conn, order_id, pprice, "paid")
    print(f"  → INELIGIBLE for damaged claim — 15 days since delivery (window: 7 days)")


def seed_shipping_delay(conn, c):
    """Order shipped 10 days ago. Estimated delivery was 5 days ago. Still in transit."""
    pid, pname, pcat, pprice = PRODUCTS["electronics"]
    user_id = insert_user(conn, c["customer_id"], c["name"], c["email"])
    insert_product(conn, pid, pname, pcat, pprice)
    order_id, _ = insert_order(conn, c["customer_id"], pid, pname, pcat,
                                pprice, "shipped",
                                created_days_ago=12)
    # estimated delivery was 5 days ago — clearly delayed
    insert_shipment(conn, order_id, "in_transit",
                    estimated_delivery_days_ago=5)
    insert_payment(conn, order_id, pprice, "paid")
    print(f"  → SHIPPING DELAY — estimated delivery was 5 days ago, still in_transit")


def seed_clean_order(conn, c):
    """Clean delivered order. No issues. Used for order status / tracking tests."""
    pid, pname, pcat, pprice = PRODUCTS["clothing"]
    user_id = insert_user(conn, c["customer_id"], c["name"], c["email"])
  
    insert_product(conn, pid, pname, pcat, pprice)
    order_id, _ = insert_order(conn, c["customer_id"],pid, pname, pcat,
                                pprice, "delivered",
                                created_days_ago=15, delivered_days_ago=10)
    insert_shipment(conn, order_id, "delivered")
    insert_payment(conn, order_id, pprice, "paid")
    print(f"  → clean delivered order — good for order_status / order_tracking tests")


# ---------------------------------------------------------------------------
# Scenario dispatch
# ---------------------------------------------------------------------------

SCENARIO_FN = {
    "eligible_refund":               seed_eligible_refund,
    "ineligible_refund_past_window":  seed_ineligible_refund,
    "eligible_exchange":             seed_eligible_exchange,
    "ineligible_exchange_past_window": seed_ineligible_exchange,
    "eligible_return_in_progress":   seed_eligible_return,
    "ineligible_return_past_window":  seed_ineligible_return,
    "eligible_damaged_within_window": seed_eligible_damaged,
    "ineligible_damaged_past_window": seed_ineligible_damaged,
    "shipping_delay":                seed_shipping_delay,
    "clean_delivered_no_issues":     seed_clean_order,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}")
        print("Run python app/db/seed.py first to initialise the schema.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        with conn:
            for c in CUSTOMERS:
                print(f"\n[{c['customer_id']}] {c['name']} — {c['scenario']}")
                SCENARIO_FN[c["scenario"]](conn, c)

        print("\n✅ Done. Test customer summary:")
        print("-" * 60)
        rows = conn.execute(
            "SELECT u.customer_id, u.name, o.id AS order_id, o.status, "
            "o.delivered_at, o.total_minor "
            "FROM users u "
            "LEFT JOIN orders o ON o.customer_id = u.customer_id "
            "WHERE u.customer_id LIKE 'C00000%' "
            "ORDER BY u.customer_id"
        ).fetchall()
        for r in rows:
            delivered = r["delivered_at"][:10] if r["delivered_at"] else "not yet"
            print(
                f"  {r['customer_id']}  {r['name']:<20}  "
                f"order={r['order_id']}  status={r['status']:<10}  "
                f"delivered={delivered}  ₹{r['total_minor']/100:.2f}"
            )

        print("\nTest credentials:")
        print("  All customers: PIN = 1234")
        print("  Reviewers:     R000001 / R000002  PIN = 9999")

    except Exception as e:
        print(f"\n❌ Seed failed: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()