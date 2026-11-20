import importlib.util
import json
import sys
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import ModuleType
from unittest.mock import patch

from fastapi.testclient import TestClient

if importlib.util.find_spec("sentence_transformers") is None:
    sentence_transformers = ModuleType("sentence_transformers")
    sentence_transformers.SentenceTransformer = type("SentenceTransformer", (), {})
    sys.modules["sentence_transformers"] = sentence_transformers

from app.db.database import execute, fetch_all, fetch_one
from app.main import app
from app.auth.session import invalidate_session


CUSTOMER_ID = "C000001"
CUSTOMER_PIN = "1234"


class AuthenticatedAPITestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.created_ticket_ids = []
        self.created_order_ids = []

        customer = fetch_one(
            "SELECT customer_id FROM users WHERE customer_id = ?",
            (CUSTOMER_ID,),
        )
        self.assertIsNotNone(customer, "Seed the database before running API tests.")

        login = self.client.post(
            "/auth/login",
            json={"customer_id": CUSTOMER_ID, "pin": CUSTOMER_PIN},
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.token = login.json()["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        for ticket_id in self.created_ticket_ids:
            runs = fetch_all(
                "SELECT id FROM workflow_runs WHERE ticket_id = ?",
                (ticket_id,),
            )
            for run in runs:
                execute("DELETE FROM audit_events WHERE run_id = ?", (run["id"],))
            execute("DELETE FROM workflow_runs WHERE ticket_id = ?", (ticket_id,))
            execute(
                "DELETE FROM audit_events "
                "WHERE event_type = 'ticket_created' AND payload LIKE ?",
                (f"%{ticket_id}%",),
            )
            execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))

        for order_id in self.created_order_ids:
            for table in ("order_items", "payments", "shipments", "returns"):
                execute(f"DELETE FROM {table} WHERE order_id = ?", (order_id,))
            execute("DELETE FROM orders WHERE id = ?", (order_id,))

        invalidate_session(self.token)
        self.client.close()

    def _create_ticket_and_start_run(self, category, message, order_id=None):
        ticket_response = self.client.post(
            "/tickets",
            headers=self.headers,
            json={
                "category": category,
                "message": message,
                "order_id": order_id,
            },
        )
        self.assertEqual(ticket_response.status_code, 201, ticket_response.text)
        ticket_id = ticket_response.json()["ticket_id"]
        self.created_ticket_ids.append(ticket_id)

        run_response = self.client.post(
            f"/tickets/{ticket_id}/runs",
            headers=self.headers,
        )
        self.assertEqual(run_response.status_code, 201, run_response.text)
        return ticket_id, run_response.json()

    def _poll_run(self, run_id):
        for _ in range(20):
            response = self.client.get(
                f"/runs/{run_id}",
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200, response.text)
            run = response.json()
            if run["status"] in {"completed", "failed", "paused"}:
                return run
            time.sleep(0.05)
        self.fail(f"Run {run_id} did not reach a terminal state.")

    def _find_or_create_order(self, status, delivered_days_ago=None):
        if delivered_days_ago is None:
            existing = fetch_one(
                "SELECT id FROM orders "
                "WHERE customer_id = ? AND status = 'confirmed' LIMIT 1",
                (CUSTOMER_ID,),
            )
            if existing:
                return existing["id"]
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            for order in fetch_all(
                "SELECT id, delivered_at FROM orders "
                "WHERE customer_id = ? AND status = 'delivered' "
                "AND delivered_at IS NOT NULL",
                (CUSTOMER_ID,),
            ):
                delivered_at = datetime.fromisoformat(
                    order["delivered_at"].replace("Z", "+00:00")
                )
                if delivered_at.tzinfo is None:
                    delivered_at = delivered_at.replace(tzinfo=timezone.utc)
                if delivered_at < cutoff:
                    return order["id"]

        order_id = f"ord_test_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        created_at = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        delivered_at = (
            (now - timedelta(days=delivered_days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if delivered_days_ago is not None
            else None
        )
        execute(
            "INSERT INTO orders "
            "(id, customer_id, status, currency, total_minor, created_at, delivered_at) "
            "VALUES (?, ?, ?, 'INR', 10000, ?, ?)",
            (order_id, CUSTOMER_ID, status, created_at, delivered_at),
        )
        self.created_order_ids.append(order_id)
        return order_id


class OrderPlacementGraphTests(AuthenticatedAPITestCase):
    @staticmethod
    def _mock_order_placement_llm(prompt):
        if "Available products (do not invent any product not in this list):" in prompt:
            offered = prompt.split(
                "Available products (do not invent any product not in this list):\n",
                1,
            )[1].split("\n\n[TASK]", 1)[0]
            products = json.loads(offered)[:3]
            return json.dumps({
                "products": [
                    {
                        **product,
                        "price_display": f"₹{product['price_minor'] / 100:.2f}",
                    }
                    for product in products
                ],
                "message": "Here are some shoes. Which would you like?",
            })

        offered = prompt.split("Customer reply:", 1)[0].rsplit(":\n", 1)[-1].strip()
        product = json.loads(offered)[0]
        return json.dumps({
            "selected_product_id": product["product_id"],
            "quantity": 1,
            "clarification_message": "",
            "needs_clarification": False,
        })

    def _start_shoes_run(self):
        return self._create_ticket_and_start_run(
            "order_placement",
            "show me shoes",
        )

    def test_order_placement_graph_does_not_write_order(self):
        before = fetch_one(
            "SELECT COUNT(*) AS order_count FROM orders WHERE customer_id = ?",
            (CUSTOMER_ID,),
        )["order_count"]

        with patch(
            "app.agents.order_placement.call_gemini",
            side_effect=self._mock_order_placement_llm,
        ):
            _, run = self._start_shoes_run()

        self.assertEqual(run["status"], "paused")
        after = fetch_one(
            "SELECT COUNT(*) AS order_count FROM orders WHERE customer_id = ?",
            (CUSTOMER_ID,),
        )["order_count"]
        self.assertEqual(after, before)

    def test_cart_item_returned_on_completion(self):
        with patch(
            "app.agents.order_placement.call_gemini",
            side_effect=self._mock_order_placement_llm,
        ):
            _, run = self._start_shoes_run()
            self.assertEqual(run["status"], "paused")

            resume = self.client.post(
                f"/runs/{run['run_id']}/messages",
                headers=self.headers,
                json={"message": "option 1"},
            )
            self.assertEqual(resume.status_code, 200, resume.text)
            completed = self._poll_run(run["run_id"])

        self.assertEqual(completed["status"], "completed")
        self.assertTrue(
            completed.get("cart_item")
            or any(
                word in (completed.get("final_response") or "").lower()
                for word in ("added", "cart")
            )
        )


class RefundIneligibilityTests(AuthenticatedAPITestCase):
    def test_confirmed_order_refund_returns_clean_message(self):
        order_id = self._find_or_create_order("confirmed")
        _, run = self._create_ticket_and_start_run(
            "refund",
            f"I want a refund for {order_id}",
            order_id=order_id,
        )
        completed = self._poll_run(run["run_id"])

        self.assertEqual(completed["status"], "completed")
        response = (completed.get("final_response") or "").lower()
        self.assertTrue("delivered" in response or "delivery" in response)
        self.assertNotIn("encountered an issue", response)

    def test_delivered_order_outside_window_returns_clean_message(self):
        order_id = self._find_or_create_order("delivered", delivered_days_ago=45)
        _, run = self._create_ticket_and_start_run(
            "refund",
            f"I want a refund for {order_id}",
            order_id=order_id,
        )
        completed = self._poll_run(run["run_id"])

        self.assertEqual(completed["status"], "completed")
        response = (completed.get("final_response") or "").lower()
        self.assertTrue(
            "30" in response or "window" in response or "days" in response
        )


if __name__ == "__main__":
    unittest.main()
