import sqlite3
from datetime import datetime, timedelta, timezone

DB = "data/customer_support.sqlite3"
conn = sqlite3.connect(DB)
conn.execute("PRAGMA foreign_keys = ON")

now       = datetime.now(timezone.utc)
delivered = (now - timedelta(days=5)).isoformat()
created   = (now - timedelta(days=12)).isoformat()
received  = (now - timedelta(days=3)).isoformat()

order_id   = "ord_tc_c000001"
item_id    = "oi_tc_c000001"
ship_id    = "shp_tc_c000001"
pay_id     = "pay_tc_c000001"
ret_id     = "ret_tc_c000001"
product_id = "prod_test_e1"
price      = 2999900

# Delete old rows in FK order
# Delete old rows in FK order (children first)
order_filter = f"order_id = '{order_id}'"
payment_filter = f"payment_id IN (SELECT id FROM payments WHERE {order_filter})"
return_filter = f"return_id IN (SELECT id FROM returns WHERE {order_filter})"
item_filter = f"order_item_id IN (SELECT id FROM order_items WHERE {order_filter})"

conn.execute(f"DELETE FROM payment_transactions WHERE {payment_filter}")
conn.execute(f"DELETE FROM refunds WHERE {order_filter}")
conn.execute(f"DELETE FROM return_items WHERE {return_filter}")
conn.execute(f"DELETE FROM exchange_items WHERE original_order_item_id IN (SELECT id FROM order_items WHERE {order_filter})")
conn.execute(f"DELETE FROM exchanges WHERE original_order_id = '{order_id}' OR replacement_order_id = '{order_id}'")
conn.execute(f"DELETE FROM returns WHERE {order_filter}")
conn.execute(f"DELETE FROM payments WHERE {order_filter}")
conn.execute(f"DELETE FROM shipments WHERE {order_filter}")
conn.execute(f"DELETE FROM order_items WHERE {order_filter}")
conn.execute(f"DELETE FROM orders WHERE id = '{order_id}'")

conn.commit()
conn.close()

print("Done. C000001 reseeded.")
print(f"  order_id  : {order_id}")
print(f"  delivered : {delivered[:10]}")
print(f"  price     : Rs.{price/100:.2f}")
print(f"  return    : received + inspection passed")