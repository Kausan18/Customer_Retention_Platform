import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch

from langgraph.types import Command

from app.agents.order_placement import (
    build_cart_response_node,
    handle_error_node,
    order_placement_graph,
    search_catalog_node,
)
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
                ("p4", "Headphones Stand", "Electronics", 3000),
                ("p5", "Wireless Headphones Case", "Clothing", 2000),
            ],
        )
        connection.commit()

        with patch.object(
            catalog_tools,
            "fetch_all",
            side_effect=lambda sql, params: connection.execute(sql, params).fetchall(),
        ):
            result = catalog_tools.search_catalog(
                query="the i want to buy some wireless headphones please"
            )
            category_result = catalog_tools.search_catalog(
                category="Electronics",
                query="the i want to buy some wireless headphones please",
            )
            category_name_result = catalog_tools.search_catalog(
                category="Electronics",
                query="electronics",
            )

        connection.close()
        self.assertEqual(
            [product["product_id"] for product in result["products"]],
            ["p5", "p1", "p4", "p2"],
        )
        self.assertEqual(
            [product["product_id"] for product in category_result["products"]],
            ["p1", "p4", "p2"],
        )
        self.assertEqual(category_name_result["products"], [])

    def test_ambiguous_selection_prompts_again_then_returns_cart_item(self):
        catalog_products = [
            {
                "product_id": "p1",
                "name": "Bluetooth Speaker One",
                "category": "Audio",
                "price_minor": 5000,
            },
            {
                "product_id": "p2",
                "name": "Bluetooth Speaker Two",
                "category": "Audio",
                "price_minor": 7000,
            },
        ]
        options_response = json.dumps({
            "products": [
                {**product, "price_display": "₹50.00"}
                for product in catalog_products
            ],
            "message": "Here are two speakers. Which would you like?",
        })
        ambiguous_response = json.dumps({
            "selected_product_id": None,
            "quantity": 1,
            "clarification_message": "We have two speakers. Which one would you like?",
            "needs_clarification": True,
        })
        selected_response = json.dumps({
            "selected_product_id": "p1",
            "quantity": 2,
            "clarification_message": "",
            "needs_clarification": False,
        })

        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        initial_state = {
            "run_id": "run_test",
            "ticket_id": "ticket_test",
            "customer_id": "customer_test",
            "customer_message": "Find bluetooth speakers",
        }

        with patch(
            "app.agents.order_placement.search_catalog",
            return_value={"success": True, "products": catalog_products},
        ), patch(
            "app.agents.order_placement.call_gemini",
            side_effect=[
                options_response,
                ambiguous_response,
                selected_response,
            ],
        ) as call_gemini, patch(
            "app.agents.order_placement.update_ticket"
        ) as update_ticket, patch(
            "app.agents.order_placement.log_event"
        ):
            initial_result = order_placement_graph.invoke(initial_state, config=config)
            self.assertIn("__interrupt__", initial_result)

            clarification_result = order_placement_graph.invoke(
                Command(resume="only one?"),
                config=config,
            )
            self.assertIn(
                "End your message by asking the customer to confirm which item they want by number or name before the order is placed. Do not place the order yet.",
                call_gemini.call_args_list[0].args[0],
            )
            self.assertIn("__interrupt__", clarification_result)
            self.assertIn(
                "We have two speakers. Which one would you like?",
                clarification_result["__interrupt__"][0].value["message"],
            )
            self.assertIn(
                'If they ask a question, say "only one?"',
                call_gemini.call_args_list[1].args[0],
            )

            completed = order_placement_graph.invoke(
                Command(resume="option 1"),
                config=config,
            )
        self.assertEqual(
            completed["cart_item"],
            {
                "product_id": "p1",
                "product_name": "Bluetooth Speaker One",
                "price_minor": 5000,
                "category": "Audio",
                "quantity": 2,
            },
        )
        self.assertEqual(
            completed["final_response"],
            "Got it! I've added Bluetooth Speaker One (₹50.00) to your cart. "
            "You can add more items or type 'checkout' to proceed to payment.",
        )
        self.assertEqual(completed["run_status"], "completed")
        self.assertEqual(call_gemini.call_count, 3)
        update_ticket.assert_called_once_with(
            ticket_id="ticket_test",
            status="resolved",
            note=completed["final_response"],
            customer_id="customer_test",
        )

    def test_build_cart_response_defaults_quantity_to_one(self):
        state = {
            "validated_product": {
                "product_id": "p1",
                "product_name": "Bluetooth Speaker One",
                "price_minor": 5000,
                "category": "Audio",
            },
        }

        result = build_cart_response_node(state)

        self.assertEqual(result["cart_item"]["quantity"], 1)
        self.assertEqual(result["run_status"], "completed")

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
