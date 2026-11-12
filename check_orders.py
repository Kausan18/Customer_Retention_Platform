# check_orders.py
from app.db.database import fetch_one, fetch_all

order = fetch_one(
    "SELECT id, customer_id, status, delivered_at FROM orders WHERE id = 'ord_0009'",
    ()
)
print("ord_0009 owner:", order)

rows = fetch_all("""
    SELECT o.id, o.status, o.delivered_at, o.total_minor,
           p.status AS payment_status
    FROM orders o
    JOIN payments p ON p.order_id = o.id
    WHERE o.customer_id = 'C211710'
      AND o.status = 'delivered'
      AND p.status = 'paid'
    ORDER BY o.delivered_at DESC
""", ())
print("\nC211710 delivered+paid orders:")
for r in rows:
    print(dict(r))