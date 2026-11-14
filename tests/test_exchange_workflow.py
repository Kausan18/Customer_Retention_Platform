import uuid
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.agents import exchange_workflow
from app.db.database import execute, fetch_all, fetch_one
from app.services.eligibility import check_exchange_eligibility


class ExchangeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.created_runs = []

    def tearDown(self):
        for run_id, ticket_id in reversed(self.created_runs):
            execute(
                "DELETE FROM approvals WHERE proposal_id IN "
                "(SELECT id FROM action_proposals WHERE run_id = ?)",
                (run_id,),
            )
            execute("DELETE FROM action_proposals WHERE run_id = ?", (run_id,))
            execute("DELETE FROM audit_events WHERE run_id = ?", (run_id,))
            execute("DELETE FROM workflow_runs WHERE id = ?", (run_id,))
            execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))

    def _find_eligible_order(self, category_predicate, require_replacement=False):
        orders = fetch_all(
            "SELECT id, customer_id FROM orders WHERE status = 'delivered' ORDER BY id"
        )
        for order in orders:
            eligibility = check_exchange_eligibility(order["id"])
            if not eligibility.get("eligible"):
                continue
            items = eligibility.get("items") or []
            categories = {item.get("category") for item in items if item.get("category")}
            if not categories or not category_predicate(categories):
                continue
            if require_replacement:
                replacement = exchange_workflow.search_catalog(
                    category=items[0]["category"],
                    exclude_product_ids=[item["product_id"] for item in items],
                )
                if not replacement.get("products"):
                    continue
            return order, eligibility
        self.fail("No eligible exchange order matched the requested category.")

    def _invoke_graph(self, order, message):
        run_id = f"run_test_{uuid.uuid4().hex}"
        ticket_id = f"ticket_test_{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).isoformat()
        execute(
            """
            INSERT INTO tickets
                (id, customer_id, order_id, category, status, summary, created_at, updated_at)
            VALUES (?, ?, ?, 'exchange', 'in_progress', ?, ?, ?)
            """,
            (
                ticket_id,
                order["customer_id"],
                order["id"],
                message,
                now,
                now,
            ),
        )
        execute(
            """
            INSERT INTO workflow_runs (id, ticket_id, thread_id, status, started_at)
            VALUES (?, ?, ?, 'running', ?)
            """,
            (run_id, ticket_id, run_id, now),
        )
        self.created_runs.append((run_id, ticket_id))

        config = {"configurable": {"thread_id": run_id}}
        initial_state = {
            "run_id": run_id,
            "ticket_id": ticket_id,
            "customer_id": order["customer_id"],
            "order_id": order["id"],
            "complaint_message": message,
            "order": None,
            "payment": None,
            "policy_passages": None,
            "eligibility_result": None,
            "catalog_results": None,
            "selected_replacement": None,
            "proposal_id": None,
            "reviewer_decision": None,
            "execution_result": None,
            "error_code": None,
            "error_message": None,
            "final_response": None,
        }
        with patch.object(
            exchange_workflow,
            "search_policy_tool",
            return_value={"success": True, "passages": []},
        ):
            result = exchange_workflow.exchange_workflow_graph.invoke(
                initial_state, config=config
            )

        graph_state = exchange_workflow.exchange_workflow_graph.get_state(config)
        status = (
            "paused"
            if graph_state.next
            else "failed"
            if result.get("error_code")
            else "completed"
        )
        execute(
            "UPDATE workflow_runs SET status = ?, completed_at = ? WHERE id = ?",
            (status, None if status == "paused" else now, run_id),
        )
        return run_id, ticket_id, result, status

    def test_cross_category_request_declines_without_proposal(self):
        order, _ = self._find_eligible_order(
            lambda categories: "Electronics" not in categories
            and bool(categories & {"Shoes", "Clothing"})
        )
        message = (
            f"I want to exchange my order {order['id']} for Electronics"
        )

        run_id, ticket_id, result, status = self._invoke_graph(order, message)

        self.assertEqual(status, "completed")
        self.assertIsNone(result.get("error_code"))
        self.assertEqual(
            fetch_all("SELECT id FROM action_proposals WHERE run_id = ?", (run_id,)),
            [],
        )
        ticket = fetch_one(
            "SELECT status, summary FROM tickets WHERE id = ?", (ticket_id,)
        )
        self.assertEqual(ticket["status"], "resolved")
        self.assertIn("category", ticket["summary"].lower())

    def test_same_category_request_proceeds_to_proposal(self):
        order, _ = self._find_eligible_order(
            lambda categories: "Electronics" in categories,
            require_replacement=True,
        )
        message = f"I want to exchange my order {order['id']} for Electronics"

        run_id, _, _, status = self._invoke_graph(order, message)

        self.assertEqual(status, "paused")
        proposals = fetch_all(
            "SELECT status FROM action_proposals WHERE run_id = ?", (run_id,)
        )
        self.assertEqual(proposals, [{"status": "pending"}])

    def test_no_category_in_message_uses_keyword_fallback(self):
        order, _ = self._find_eligible_order(
            lambda categories: bool(categories),
            require_replacement=True,
        )

        run_id, _, _, status = self._invoke_graph(
            order, "I want to exchange my item for a similar one"
        )

        self.assertEqual(status, "paused")
        proposals = fetch_all(
            "SELECT status FROM action_proposals WHERE run_id = ?", (run_id,)
        )
        self.assertEqual(proposals, [{"status": "pending"}])

    def test_furniture_request_declines_cross_category(self):
        order, _ = self._find_eligible_order(lambda categories: bool(categories))
        message = f"I want to exchange order {order['id']} for a bicycle"

        run_id, ticket_id, result, status = self._invoke_graph(order, message)

        self.assertEqual(status, "completed")
        self.assertIsNone(result.get("error_code"))
        self.assertEqual(
            fetch_all("SELECT id FROM action_proposals WHERE run_id = ?", (run_id,)),
            [],
        )
        ticket = fetch_one(
            "SELECT status, summary FROM tickets WHERE id = ?", (ticket_id,)
        )
        self.assertEqual(ticket["status"], "resolved")
        self.assertNotEqual(ticket["summary"], message)


if __name__ == "__main__":
    unittest.main()
