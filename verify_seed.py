"""
verify_seed.py

Run after app/db/seed.py to confirm the database looks right:
row counts per table, a sample record from each, and a few
integrity checks (orphaned rows, total mismatches).

Run from the project root:
    python verify_seed.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "customer_support.sqlite3"

TABLES = [
    "product_catalog", "orders", "order_items", "shipments",
    "payments", "returns", "tickets", "workflow_runs",
    "action_proposals", "approvals", "audit_events",
]


def main() -> None:
    if not DB_PATH.exists():
        print(f"[FAIL] Database not found at {DB_PATH}. Run the seed script first.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print(f"Database: {DB_PATH}\n")
    print("=== Row counts ===")
    for table in TABLES:
        try:
            count = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<20} {count}")
        except sqlite3.OperationalError as exc:
            print(f"  {table:<20} MISSING ({exc})")

    print("\n=== Sample records ===")
    for table in ["product_catalog", "orders", "order_items", "shipments", "payments", "returns"]:
        row = cur.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()
        print(f"  {table}:")
        print(f"    {dict(row) if row else '(empty)'}")

    print("\n=== Order status distribution ===")
    for row in cur.execute("SELECT status, COUNT(*) c FROM orders GROUP BY status ORDER BY c DESC"):
        print(f"  {row['status']:<12} {row['c']}")

    print("\n=== Integrity checks ===")

    orphan_items = cur.execute("""
        SELECT COUNT(*) FROM order_items oi
        LEFT JOIN product_catalog p ON p.id = oi.product_id
        WHERE p.id IS NULL
    """).fetchone()[0]
    print(f"  Order items with missing product:  {orphan_items} (expect 0)")

    orphan_orders_items = cur.execute("""
        SELECT COUNT(*) FROM order_items oi
        LEFT JOIN orders o ON o.id = oi.order_id
        WHERE o.id IS NULL
    """).fetchone()[0]
    print(f"  Order items with missing order:    {orphan_orders_items} (expect 0)")

    total_mismatches = cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT o.id, o.total_minor, SUM(oi.price_minor * oi.quantity) AS calc
            FROM orders o JOIN order_items oi ON oi.order_id = o.id
            GROUP BY o.id
            HAVING o.total_minor != calc
        )
    """).fetchone()[0]
    print(f"  Orders with total/item mismatch:   {total_mismatches} (expect 0)")

    delivered_without_shipment = cur.execute("""
        SELECT COUNT(*) FROM orders o
        LEFT JOIN shipments s ON s.order_id = o.id
        WHERE s.id IS NULL
    """).fetchone()[0]
    print(f"  Orders with no shipment:           {delivered_without_shipment} (expect 0)")

    orders_without_payment = cur.execute("""
        SELECT COUNT(*) FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE p.id IS NULL
    """).fetchone()[0]
    print(f"  Orders with no payment:            {orders_without_payment} (expect 0)")

    returns_on_undelivered = cur.execute("""
        SELECT COUNT(*) FROM returns r
        JOIN orders o ON o.id = r.order_id
        WHERE o.status != 'delivered'
    """).fetchone()[0]
    print(f"  Returns on undelivered orders:     {returns_on_undelivered} (expect 0)")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()