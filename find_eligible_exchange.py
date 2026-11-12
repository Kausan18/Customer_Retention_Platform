# find_eligible_exchange.py
from app.db.database import fetch_all
from datetime import datetime, timedelta, timezone

now = datetime.now(timezone.utc)
cutoff = now - timedelta(days=15)

rows = fetch_all("""
    SELECT o.id, o.customer_id, o.status, o.delivered_at,
           o.total_minor, p.status AS payment_status
    FROM orders o
    JOIN payments p ON p.order_id = o.id
    WHERE o.status = 'delivered'
      AND p.status = 'paid'
      AND o.delivered_at >= ?
    ORDER BY o.delivered_at DESC
    LIMIT 10
""", (cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),))

if not rows:
    print("No eligible exchange orders found within the 15-day exchange window.")
else:
    for r in rows:
        print(dict(r))