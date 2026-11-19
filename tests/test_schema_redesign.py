import importlib.util
import sys
import unittest
from types import ModuleType

from fastapi.testclient import TestClient

if importlib.util.find_spec("sentence_transformers") is None:
    sentence_transformers = ModuleType("sentence_transformers")
    sentence_transformers.SentenceTransformer = type("SentenceTransformer", (), {})
    sys.modules["sentence_transformers"] = sentence_transformers

from app.auth.session import _clear_all_sessions_for_testing
from app.db.database import fetch_all, fetch_one, get_connection
from app.main import app


class SchemaTablesExistTests(unittest.TestCase):
    def test_all_new_tables_exist(self):
        expected_tables = {
            "users",
            "addresses",
            "exchanges",
            "exchange_items",
            "return_items",
            "payment_transactions",
            "refunds",
        }
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        table_names = {row["name"] for row in rows}
        self.assertTrue(expected_tables.issubset(table_names))

    def test_orders_has_shipping_address_id_column(self):
        with get_connection() as conn:
            columns = conn.execute("PRAGMA table_info(orders)").fetchall()
        self.assertIn("shipping_address_id", {column["name"] for column in columns})


class UsersSeedTests(unittest.TestCase):
    def test_customer_count(self):
        row = fetch_one("SELECT COUNT(*) AS count FROM users WHERE role = 'customer'")
        self.assertEqual(row["count"], 50)

    def test_reviewer_count(self):
        row = fetch_one("SELECT COUNT(*) AS count FROM users WHERE role = 'reviewer'")
        self.assertEqual(row["count"], 2)

    def test_customer_ids_are_unique(self):
        row = fetch_one(
            "SELECT COUNT(*) AS total, COUNT(DISTINCT customer_id) AS unique_count "
            "FROM users"
        )
        self.assertEqual(row["unique_count"], row["total"])

    def test_every_customer_has_address(self):
        row = fetch_one("""
            SELECT COUNT(*) AS count FROM users u
            WHERE u.role = 'customer'
              AND NOT EXISTS (
                  SELECT 1 FROM addresses a WHERE a.user_id = u.id
              )
        """)
        self.assertEqual(row["count"], 0)

    def test_reviewer_pin_is_9999(self):
        reviewers = fetch_all(
            "SELECT pin FROM users WHERE role = 'reviewer' ORDER BY customer_id"
        )
        self.assertEqual(len(reviewers), 2)
        self.assertEqual([row["pin"] for row in reviewers], ["9999", "9999"])

    def test_customer_pin_is_1234(self):
        customers = fetch_all(
            "SELECT pin FROM users WHERE role = 'customer' ORDER BY RANDOM() LIMIT 5"
        )
        self.assertEqual(len(customers), 5)
        self.assertTrue(all(row["pin"] == "1234" for row in customers))


class ProductCatalogTests(unittest.TestCase):
    categories = (
        "Electronics", "Clothing", "Cosmetics", "Food", "Shoes", "Toys",
    )

    def test_total_product_count(self):
        row = fetch_one("SELECT COUNT(*) AS count FROM product_catalog")
        self.assertEqual(row["count"], 300)

    def test_50_per_category(self):
        for category in self.categories:
            with self.subTest(category=category):
                row = fetch_one(
                    "SELECT COUNT(*) AS count FROM product_catalog WHERE category = ?",
                    (category,),
                )
                self.assertEqual(row["count"], 50)

    def test_out_of_stock_items_per_category(self):
        for category in self.categories:
            with self.subTest(category=category):
                row = fetch_one(
                    "SELECT COUNT(*) AS count FROM product_catalog "
                    "WHERE category = ? AND in_stock = 0",
                    (category,),
                )
                self.assertEqual(row["count"], 5)


class OrderOwnershipTests(unittest.TestCase):
    def test_all_orders_reference_valid_customer(self):
        row = fetch_one("""
            SELECT COUNT(*) AS count
            FROM orders o
            WHERE NOT EXISTS (
                SELECT 1 FROM users u WHERE u.customer_id = o.customer_id
            )
        """)
        self.assertEqual(row["count"], 0)


class AuthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        _clear_all_sessions_for_testing()

    def tearDown(self):
        self.client.close()
        _clear_all_sessions_for_testing()

    def test_customer_login_succeeds(self):
        response = self.client.post(
            "/auth/login",
            json={"customer_id": "C000001", "pin": "1234"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("token", response.json())

    def test_reviewer_blocked_at_customer_login(self):
        response = self.client.post(
            "/auth/login",
            json={"customer_id": "R000001", "pin": "9999"},
        )
        self.assertEqual(response.status_code, 403)

    def test_reviewer_login_succeeds(self):
        response = self.client.post(
            "/auth/reviewer-login",
            json={"customer_id": "R000001", "pin": "9999"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("reviewer_id", response.json())

    def test_wrong_pin_blocked(self):
        response = self.client.post(
            "/auth/login",
            json={"customer_id": "C000001", "pin": "0000"},
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
