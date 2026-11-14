import sqlite3
import unittest
from unittest.mock import patch

from app.agents.order_placement import handle_error_node, search_catalog_node
from app.tools import catalog_tools


class CatalogSearchTests(unittest.TestCase):
    def test_extract_catalog_keywords_applies_length_and_stop_list(self):
        keywords = catalog_tools.extract_catalog_keywords(
            "I want something from the shoes section under 2000 rupees"
        )
        self.assertEqual(keywords, ["something", "shoes", "section", "rupees"])

    def test_multiword_search_matches_and_ranks_by_relevance(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE product_catalog (
                id TEXT, name TEXT, category TEXT, price_minor INTEGER, in_stock INTEGER
            )
            """
        )
        connection.executemany(
            "INSERT INTO product_catalog VALUES (?, ?, ?, ?, 1)",
            [
                ("p1", "Wireless Headphones", "Electronics", 10000),
                ("p2", "Wireless Charger", "Electronics", 5000),
                ("p3", "Running Shoes", "Shoes", 7000),
            ],
        )
        connection.commit()

        with patch.object(
            catalog_tools,
            "fetch_all",
            side_effect=lambda sql, params: connection.execute(sql, params).fetchall(),
        ):
            result = catalog_tools.search_catalog(query="wireless headphones")

        connection.close()
        self.assertEqual(
            [product["product_id"] for product in result["products"]],
            ["p1", "p2"],
        )

    def test_missing_product_returns_completed_customer_facing_outcome(self):
        state = {
            "run_id": "run_test",
            "ticket_id": "ticket_test",
            "customer_id": "customer_test",
            "customer_message": "Buy me laptop",
        }
        with patch(
            "app.agents.order_placement.search_catalog",
            return_value={"success": True, "products": []},
        ) as search:
            result = search_catalog_node(state)

        self.assertEqual(result["error_code"], "NO_CATALOG_RESULTS")
        self.assertEqual(search.call_count, 2)

        with patch("app.agents.order_placement.update_ticket") as update_ticket, patch(
            "app.agents.order_placement.log_event"
        ):
            completed = handle_error_node(result)

        update_ticket.assert_called_once()
        self.assertEqual(update_ticket.call_args.kwargs["status"], "resolved")
        self.assertIsNone(completed["error_code"])
        self.assertIn("browse the available categories", completed["final_response"])


if __name__ == "__main__":
    unittest.main()
