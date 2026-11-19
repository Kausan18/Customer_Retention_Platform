import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch

from langgraph.types import Command

from app.agents.order_placement import (
    create_order_node,
    handle_error_node,
    order_placement_graph,
    process_payment_node,
    route_after,
    search_catalog_node,
)
from mock_services import payment_service
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

    def test_ambiguous_selection_prompts_again_without_placing_order(self):
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
                "Your order is confirmed.",
            ],
        ) as call_gemini, patch(
            "app.agents.order_placement.place_order",
            return_value={
                "success": True,
                "order_id": "order_test",
                "created_at": "2026-11-16T00:00:00Z",
                "total_minor": 10000,
                "items": [{
                    "product_id": "p1",
                    "product_name": "Bluetooth Speaker One",
                    "quantity": 2,
                    "price_minor": 5000,
                }],
            },
        ) as place_order, patch(
            "app.agents.order_placement.update_ticket"
        ), patch(
            "app.agents.order_placement.log_event"
        ), patch.object(
            payment_service,
            "charge",
            wraps=payment_service.charge,
        ) as charge:
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
            place_order.assert_not_called()

            payment_summary = order_placement_graph.invoke(
                Command(resume="option 1"),
                config=config,
            )
            self.assertIn("__interrupt__", payment_summary)
            self.assertEqual(
                payment_summary["__interrupt__"][0].value["stage"],
                "payment_confirmation",
            )
            self.assertIn(
                "Type 'confirm' to place your order",
                payment_summary["__interrupt__"][0].value["message"],
            )
            place_order.assert_not_called()
            charge.assert_not_called()

            completed = order_placement_graph.invoke(
                Command(resume="confirm"),
                config=config,
            )

        place_order.assert_called_once_with(
            customer_id="customer_test",
            items=[{"product_id": "p1", "quantity": 2}],
        )
        charge.assert_called_once_with(
            order_id=None,
            amount_minor=10000,
            payment_method="mock_card",
        )
        self.assertEqual(completed["new_order_id"], "order_test")
        self.assertEqual(completed["order_result"]["created_at"], "2026-11-16T00:00:00Z")
        self.assertEqual(completed["order_result"]["total_minor"], 10000)
        self.assertEqual(
            completed["order_result"]["items"][0]["product_name"],
            "Bluetooth Speaker One",
        )
        self.assertEqual(
            completed["payment_result"]["method"],
            "Mock Card •••• 4242",
        )
        final_prompt = call_gemini.call_args_list[3].args[0]
        self.assertIn("Order result:", final_prompt)
        self.assertIn("Payment result:", final_prompt)
        self.assertIn(
            "Total amount paid in INR (convert from paise: divide by 100)",
            final_prompt,
        )

    def test_payment_cancellation_does_not_authorize_or_create_order(self):
        catalog_products = [
            {
                "product_id": "p1",
                "name": "Bluetooth Speaker One",
                "category": "Audio",
                "price_minor": 5000,
            },
        ]
        options_response = json.dumps({
            "products": [
                {**catalog_products[0], "price_display": "₹50.00"},
            ],
            "message": "Here is a speaker. Which would you like?",
        })
        selected_response = json.dumps({
            "selected_product_id": "p1",
            "quantity": 1,
            "clarification_message": "",
            "needs_clarification": False,
        })
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        initial_state = {
            "run_id": "run_cancel_test",
            "ticket_id": "ticket_cancel_test",
            "customer_id": "customer_test",
            "customer_message": "Find a bluetooth speaker",
        }

        with patch(
            "app.agents.order_placement.search_catalog",
            return_value={"success": True, "products": catalog_products},
        ), patch(
            "app.agents.order_placement.call_gemini",
            side_effect=[options_response, selected_response],
        ), patch(
            "app.agents.order_placement.place_order"
        ) as place_order, patch.object(
            payment_service,
            "charge",
            wraps=payment_service.charge,
        ) as charge, patch(
            "app.agents.order_placement.update_ticket"
        ) as update_ticket, patch(
            "app.agents.order_placement.log_event"
        ):
            initial_result = order_placement_graph.invoke(initial_state, config=config)
            self.assertIn("__interrupt__", initial_result)

            summary_result = order_placement_graph.invoke(
                Command(resume="option 1"),
                config=config,
            )
            self.assertIn("__interrupt__", summary_result)

            cancelled = order_placement_graph.invoke(
                Command(resume="cancel"),
                config=config,
            )

        place_order.assert_not_called()
        charge.assert_not_called()
        self.assertEqual(
            cancelled["final_response"],
            "Order cancelled. Let me know if you'd like to try again.",
        )
        self.assertIsNone(cancelled["error_code"])
        self.assertEqual(update_ticket.call_args.kwargs["status"], "resolved")

    def test_payment_failure_routes_to_error_without_creating_order(self):
        state = {
            "customer_id": "customer_test",
            "catalog_results": [
                {
                    "product_id": "p1",
                    "name": "Bluetooth Speaker One",
                    "category": "Audio",
                    "price_minor": 5000,
                },
            ],
            "selected_product_id": "p1",
            "quantity": 1,
        }
        previous_mode = payment_service.get_mode()
        payment_service.set_mode("failure")
        try:
            result = process_payment_node(state)
        finally:
            payment_service.set_mode(previous_mode)

        self.assertFalse(result["payment_authorised"])
        self.assertEqual(result["error_code"], "PAYMENT_FAILED")
        self.assertEqual(route_after(result), "handle_error_node")

        with patch("app.agents.order_placement.place_order") as place_order:
            blocked = create_order_node({
                **state,
                "payment_authorised": False,
            })

        place_order.assert_not_called()
        self.assertEqual(blocked["error_code"], "PAYMENT_FAILED")

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
