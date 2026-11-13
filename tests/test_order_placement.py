import unittest
import uuid
from app.tools.catalog_tools import place_order
from app.db.database import fetch_one, execute


class PlaceOrderTests(unittest.TestCase):
    def setUp(self):
        row = fetch_one("SELECT id, price_minor FROM product_catalog WHERE in_stock = 1 LIMIT 1", ())
        self.assertIsNotNone(row, "No in-stock product found — seed the DB first.")
        self.product_id = row["id"]
        self.price_minor = row["price_minor"]
        self.customer_id = "C211710"
        self.created_order_ids = []

    def tearDown(self):
        for oid in self.created_order_ids:
            execute("DELETE FROM order_items WHERE order_id = ?", (oid,))
            execute("DELETE FROM payments WHERE order_id = ?", (oid,))
            execute("DELETE FROM shipments WHERE order_id = ?", (oid,))
            execute("DELETE FROM orders WHERE id = ?", (oid,))

    def test_successful_order_creates_all_rows(self):
        result = place_order(self.customer_id, [{"product_id": self.product_id, "quantity": 2}])
        self.assertTrue(result["success"])
        self.created_order_ids.append(result["order_id"])

        self.assertEqual(result["total_minor"], self.price_minor * 2)

        order = fetch_one("SELECT * FROM orders WHERE id = ?", (result["order_id"],))
        self.assertIsNotNone(order)
        self.assertEqual(order["customer_id"], self.customer_id)

        payment = fetch_one("SELECT * FROM payments WHERE order_id = ?", (result["order_id"],))
        self.assertEqual(payment["status"], "paid")
        self.assertEqual(payment["amount_minor"], result["total_minor"])

        shipment = fetch_one("SELECT * FROM shipments WHERE order_id = ?", (result["order_id"],))
        self.assertEqual(shipment["status"], "processing")

    def test_nonexistent_product_fails(self):
        result = place_order(self.customer_id, [{"product_id": "prod_does_not_exist", "quantity": 1}])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "PRODUCT_NOT_FOUND")

    def test_out_of_stock_product_fails(self):
        execute("UPDATE product_catalog SET in_stock = 0 WHERE id = ?", (self.product_id,))
        try:
            result = place_order(self.customer_id, [{"product_id": self.product_id, "quantity": 1}])
            self.assertFalse(result["success"])
            self.assertEqual(result["error_code"], "OUT_OF_STOCK")
        finally:
            execute("UPDATE product_catalog SET in_stock = 1 WHERE id = ?", (self.product_id,))

    def test_empty_items_fails(self):
        result = place_order(self.customer_id, [])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "NO_ITEMS")

    def test_price_is_server_controlled_not_caller_supplied(self):
        # Even if a caller tried to pass a price, place_order ignores it
        # and re-reads from product_catalog.
        result = place_order(self.customer_id, [{"product_id": self.product_id, "quantity": 1, "price_minor": 1}])
        self.assertTrue(result["success"])
        self.created_order_ids.append(result["order_id"])
        self.assertEqual(result["items"][0]["price_minor"], self.price_minor)


if __name__ == "__main__":
    unittest.main()