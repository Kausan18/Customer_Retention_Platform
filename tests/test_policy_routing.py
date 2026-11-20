import unittest
from unittest.mock import patch

from app.api import tickets
from app.schemas.ticket import TicketCreate
from frontend.utils.detect_category import detect_category


class PolicyRoutingTests(unittest.TestCase):
    def test_policy_question_bypasses_intent_validation_and_returns_answer(self):
        question = "How do I return this item?"
        ticket = {
            "id": "ticket_policy_test",
            "customer_id": "customer_test",
            "category": "order_status",
            "status": "open",
            "summary": question,
        }
        completed_at = "2026-11-16T00:00:00+00:00"

        with patch.object(
            tickets,
            "fetch_one",
            side_effect=[
                ticket,
                {"status": "completed", "completed_at": completed_at},
            ],
        ), patch.object(tickets, "execute") as execute, patch.object(
            tickets.audit, "log_event"
        ), patch.object(tickets, "validate_intent") as validate_intent, patch(
            "app.main.answer_policy_question",
            return_value="Refunds are handled according to the policy.",
        ) as answer_policy_question, patch.object(
            tickets, "run_workflow"
        ) as run_workflow:
            response = tickets.start_run("ticket_policy_test", "customer_test")

        validate_intent.assert_not_called()
        run_workflow.assert_not_called()
        answer_policy_question.assert_called_once_with(question)
        self.assertEqual(response.status, "completed")
        self.assertEqual(
            response.final_response,
            "Refunds are handled according to the policy.",
        )
        self.assertTrue(
            any(
                "UPDATE tickets SET summary" in call.args[0]
                for call in execute.call_args_list
            )
        )
        self.assertTrue(
            any(
                "UPDATE workflow_runs SET status = 'completed'" in call.args[0]
                for call in execute.call_args_list
            )
        )

    def test_policy_keywords_select_policy_category(self):
        self.assertEqual(
            detect_category("What is the refund policy?"),
            "policy",
        )

    def test_product_requests_route_to_order_placement(self):
        requests = (
            "Show me options on a keyboard",
            "I'd like to buy speakers",
            "Buy laptops",
            "Browse headphones",
            "Get me a jacket",
        )
        for request in requests:
            with self.subTest(request=request):
                self.assertEqual(detect_category(request), "order_placement")

    def test_refund_and_exchange_categories_precede_product_keywords(self):
        self.assertEqual(detect_category("I want a refund for headphones"), "refund")
        self.assertEqual(detect_category("I need an exchange for a jacket"), "exchange")
        self.assertEqual(
            detect_category("I want a refund or exchange for headphones"),
            "refund",
        )

    def test_return_and_general_questions_do_not_become_order_placement(self):
        self.assertEqual(detect_category("Return my order"), "unknown")
        self.assertEqual(
            detect_category("How can I place an order?"),
            "unknown",
        )
        self.assertEqual(
            detect_category("I want to order headphones"),
            "order_placement",
        )

    def test_tracking_status_confirmations_and_default(self):
        self.assertEqual(detect_category("Where is my order?!"), "order_tracking")
        self.assertEqual(detect_category("What is my payment status?"), "order_status")
        self.assertEqual(detect_category("option 1"), "unknown")
        self.assertEqual(detect_category("Sounds good!"), "unknown")
        self.assertEqual(detect_category("Can you help me?"), "unknown")

    def test_damaged_category_creates_support_ticket_without_workflow(self):
        body = TicketCreate(category="damaged", message="The item arrived broken")
        self.assertEqual(body.category, "damaged")
        ticket = {
            "id": "ticket_damaged",
            "customer_id": "customer_test",
            "category": "damaged",
            "status": "open",
            "summary": body.message,
        }

        with patch.object(tickets, "fetch_one", return_value=ticket), self.assertRaises(
            tickets.HTTPException
        ) as raised:
            tickets.start_run("ticket_damaged", customer_id="customer_test")

        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
