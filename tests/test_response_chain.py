"""
tests/test_response_chain.py

Contract test for the full response chain.

Asserts that tickets.summary is non-empty after every workflow reaches
a terminal state. This is the single source of truth for whether
final_response will appear in the UI -- if tickets.summary is empty here,
the bug is in the graph or the dispatcher, not in the frontend.

Each test:
1. Creates a fresh ticket + workflow_run row in the DB
2. Invokes the graph directly -- no HTTP layer
3. Queries tickets.summary and workflow_runs.status
4. Asserts the chain is intact

Test customers used (seeded by scripts/seed_test_customers.py):
  C000001 -- eligible refund (Sony WH-1000XM5, delivered 5 days ago, return passed)
  C000002 -- ineligible refund (delivered 45 days ago, outside window)
  C000003 -- eligible exchange (Nike Air Max 270, delivered 5 days ago)
  C000004 -- ineligible exchange (delivered 20 days ago, outside window)
  C000009 -- shipping delay (in_transit, past estimated delivery)
  C000010 -- clean delivered order (Levi's Jeans, delivered 10 days ago)
"""

import hashlib
import json
import unittest
import unittest.mock as mock
import uuid
from datetime import datetime, timedelta, timezone

from app.db.database import execute, fetch_one, fetch_all
from app.agents.order_tracking import order_tracking_graph
from app.agents.order_status import order_status_graph
from app.agents.refund_workflow import refund_workflow_graph
from app.agents.exchange_workflow import exchange_workflow_graph
from app.agents.order_placement import order_placement_graph
from app.services.audit import log_event
from langgraph.types import Command


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(customer_id: str, category: str, order_id: str | None,
                 message: str) -> str:
    """Insert a ticket row and return ticket_id."""
    ticket_id = f"tkt_test_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    execute(
        """INSERT INTO tickets (id, customer_id, order_id, category, status,
                summary, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'open', NULL, ?, ?)""",
        (ticket_id, customer_id, order_id, category, now, now),
    )
    return ticket_id


def _make_run(ticket_id: str) -> tuple[str, str]:
    """Insert a workflow_run row and return (run_id, thread_id)."""
    run_id = f"run_test_{uuid.uuid4().hex[:8]}"
    thread_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    execute(
        """INSERT INTO workflow_runs (id, ticket_id, thread_id, status, started_at)
           VALUES (?, ?, ?, 'running', ?)""",
        (run_id, ticket_id, thread_id, now),
    )
    return run_id, thread_id


def _get_ticket_summary(ticket_id: str) -> str | None:
    row = fetch_one("SELECT summary FROM tickets WHERE id = ?", (ticket_id,))
    return row["summary"] if row else None


def _get_run_status(run_id: str) -> str | None:
    row = fetch_one("SELECT status FROM workflow_runs WHERE id = ?", (run_id,))
    return row["status"] if row else None


def _get_order_id_for_customer(customer_id: str) -> str | None:
    # Test customers always have a seeded order with ID ord_tc_{customer_id.lower()}.
    # Query by that exact ID first; fall back to LIMIT 1 for non-test customers.
    test_order_id = f"ord_tc_{customer_id.lower()}"
    row = fetch_one("SELECT id FROM orders WHERE id = ?", (test_order_id,))
    if row:
        return row["id"]
    row = fetch_one(
        "SELECT id FROM orders WHERE customer_id = ? LIMIT 1",
        (customer_id,),
    )
    return row["id"] if row else None


def _invoke_graph(graph, initial_state: dict, thread_id: str) -> dict:
    """Invoke a LangGraph graph with the given thread_id config."""
    cfg = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke(initial_state, config=cfg)
    return result


def _cleanup_test_rows(*ticket_ids: str):
    """Remove test rows so reruns stay clean."""
    for ticket_id in ticket_ids:
        run_rows = fetch_all(
            "SELECT id FROM workflow_runs WHERE ticket_id = ?", (ticket_id,)
        )
        for r in run_rows:
            execute("DELETE FROM audit_events WHERE run_id = ?", (r["id"],))
            # approvals FK references action_proposals -- delete child first
            proposal_rows = fetch_all(
                "SELECT id FROM action_proposals WHERE run_id = ?", (r["id"],)
            )
            for p in proposal_rows:
                execute(
                    "DELETE FROM refunds WHERE proposal_id = ?", (p["id"],)
                )
                execute(
                    "DELETE FROM approvals WHERE proposal_id = ?", (p["id"],)
                )
            execute(
                "DELETE FROM action_proposals WHERE run_id = ?", (r["id"],)
            )
            execute("DELETE FROM workflow_runs WHERE id = ?", (r["id"],))
        execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class ResponseChainTestCase(unittest.TestCase):
    """
    Base: each test creates its own ticket + run, runs the graph,
    and asserts the chain. tearDown cleans up test rows.
    """

    def setUp(self):
        self._created_ticket_ids: list[str] = []
        self._gemini_patchers = []
        self._gemini_mocks = {}

        targets = {
            "order_tracking": "app.agents.order_tracking.call_gemini",
            "order_status": "app.agents.order_status.call_gemini",
            "order_placement": "app.agents.order_placement.call_gemini",
            "refund": "app.agents.refund_workflow.call_gemini",
            "exchange": "app.agents.exchange_workflow.call_gemini",
        }
        default_response = (
            '{"explanation": "Your order is on its way and will arrive soon."}'
        )

        for name, target in targets.items():
            patcher = mock.patch(target, return_value=default_response)
            self._gemini_mocks[name] = patcher.start()
            self._gemini_patchers.append(patcher)

        # Convenience alias for tests that only need the tracking mock
        self._mock_gemini = self._gemini_mocks["order_tracking"]

    def tearDown(self):
        for patcher in self._gemini_patchers:
            patcher.stop()
        if self._created_ticket_ids:
            _cleanup_test_rows(*self._created_ticket_ids)

    def _register(self, ticket_id: str) -> str:
        self._created_ticket_ids.append(ticket_id)
        return ticket_id

    def assert_response_chain(self, ticket_id: str, run_id: str,
                               expected_run_status: str = "completed"):
        summary = _get_ticket_summary(ticket_id)
        run_status = _get_run_status(run_id)

        self.assertIsNotNone(
            summary,
            f"tickets.summary is NULL for ticket {ticket_id} -- "
            "graph did not call update_ticket() or update_ticket() did not "
            "write to tickets.summary",
        )
        self.assertNotEqual(
            summary.strip(), "",
            f"tickets.summary is empty string for ticket {ticket_id}",
        )
        self.assertEqual(
            run_status, expected_run_status,
            f"workflow_runs.status = '{run_status}', "
            f"expected '{expected_run_status}' for run {run_id}",
        )


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

class TestOrderTrackingChain(ResponseChainTestCase):
    """C000009 -- shipping delay (in_transit)."""

    CUSTOMER = "C000009"

    def test_order_tracking_populates_summary(self):
        order_id = _get_order_id_for_customer(self.CUSTOMER)
        self.assertIsNotNone(order_id, "No order found for C000009")

        ticket_id = self._register(
            _make_ticket(self.CUSTOMER, "order_tracking", order_id,
                         "where is my order?")
        )
        run_id, thread_id = _make_run(ticket_id)
        log_event(run_id, self.CUSTOMER, "workflow_started", None)

        initial_state = {
            "run_id": run_id,
            "ticket_id": ticket_id,
            "customer_id": self.CUSTOMER,
            "order_id": order_id,
            "customer_message": "where is my order?",
            "order": None,
            "shipment": None,
            "error_code": None,
            "final_response": None,
        }

        self._gemini_mocks["order_tracking"].return_value = (
            '{"explanation": "Your order is in transit and will arrive soon."}'
        )
        _invoke_graph(order_tracking_graph, initial_state, thread_id)
        self.assert_response_chain(ticket_id, run_id)


class TestOrderStatusChain(ResponseChainTestCase):
    """C000010 -- clean delivered order."""

    CUSTOMER = "C000010"

    def test_order_status_populates_summary(self):
        order_id = _get_order_id_for_customer(self.CUSTOMER)
        self.assertIsNotNone(order_id, "No order found for C000010")

        ticket_id = self._register(
            _make_ticket(self.CUSTOMER, "order_status", order_id,
                         "what is the status of my order?")
        )
        run_id, thread_id = _make_run(ticket_id)
        log_event(run_id, self.CUSTOMER, "workflow_started", None)

        initial_state = {
            "run_id": run_id,
            "ticket_id": ticket_id,
            "customer_id": self.CUSTOMER,
            "order_id": order_id,
            "customer_message": "what is the status of my order?",
            "order": None,
            "payment": None,
            "error_code": None,
            "final_response": None,
        }

        self._gemini_mocks["order_status"].return_value = (
            '{"summary": "Your order has been delivered and payment is confirmed."}'
        )
        _invoke_graph(order_status_graph, initial_state, thread_id)
        self.assert_response_chain(ticket_id, run_id)


class TestRefundChain(ResponseChainTestCase):
    """
    Two refund terminal states:
      1. Eligible + approved  -> completed, payments.refunded_minor updated
      2. Ineligible (outside window) -> completed, no payment change
    """

    ELIGIBLE_CUSTOMER = "C000001"
    INELIGIBLE_CUSTOMER = "C000002"

    def _get_refundable_amount(self, order_id: str) -> int:
        row = fetch_one(
            "SELECT amount_minor - refunded_minor AS refundable "
            "FROM payments WHERE order_id = ?",
            (order_id,),
        )
        return row["refundable"] if row else 0

    def test_eligible_refund_populates_summary_and_updates_payment(self):
        order_id = _get_order_id_for_customer(self.ELIGIBLE_CUSTOMER)
        self.assertIsNotNone(order_id, "No order for C000001")

        refundable = self._get_refundable_amount(order_id)

        ticket_id = self._register(
            _make_ticket(self.ELIGIBLE_CUSTOMER, "refund", order_id,
                         "i want a refund")
        )
        run_id, thread_id = _make_run(ticket_id)
        log_event(run_id, self.ELIGIBLE_CUSTOMER, "workflow_started", None)

        initial_state = {
            "run_id": run_id,
            "ticket_id": ticket_id,
            "customer_id": self.ELIGIBLE_CUSTOMER,
            "order_id": order_id,
            "customer_message": "i want a refund",
            "order": None,
            "payment": None,
            "return_record": None,
            "policy_passages": [],
            "eligibility_result": None,
            "proposal_id": None,
            "reviewer_decision": None,
            "execution_result": None,
            "error_code": None,
            "final_response": None,
        }

        execute(
            "UPDATE payments SET status = 'paid', refunded_minor = 0 "
            "WHERE order_id = ?",
            (order_id,),
        )

        # Phase 1: graph runs until interrupt at await_approval_node
        cfg = {"configurable": {"thread_id": thread_id}}
        refund_workflow_graph.invoke(initial_state, config=cfg)

        proposal_row = fetch_one(
            "SELECT id, payload, payload_hash, status FROM action_proposals "
            "WHERE run_id = ? AND status = 'pending'",
            (run_id,),
        )
        self.assertIsNotNone(
            proposal_row,
            "No pending proposal created by refund graph -- "
            "graph did not reach create_proposal_node or proposal was not committed",
        )
        proposal_id = proposal_row["id"]

        # Simulate reviewer approval
        now = datetime.now(timezone.utc).isoformat()
        execute(
            "UPDATE action_proposals SET status = 'approved' WHERE id = ?",
            (proposal_id,),
        )
        approval_row = fetch_one(
            "SELECT id FROM approvals WHERE proposal_id = ?", (proposal_id,)
        )
        if approval_row:
            execute(
                "UPDATE approvals SET decision = 'approved', decided_at = ? "
                "WHERE id = ?",
                (now, approval_row["id"]),
            )
        else:
            approval_id = f"apv_test_{uuid.uuid4().hex[:8]}"
            execute(
                """INSERT INTO approvals
                       (id, proposal_id, reviewer_id, decision, decided_at, created_at)
                   VALUES (?, ?, 'R000001', 'approved', ?, ?)""",
                (approval_id, proposal_id, now, now),
            )

        # Phase 2: resume the graph
        refund_workflow_graph.invoke(
            Command(resume={"decision": "approved", "proposal_id": proposal_id}),
            config=cfg,
        )

        self.assert_response_chain(ticket_id, run_id)

        row = fetch_one(
            "SELECT refunded_minor FROM payments WHERE order_id = ?",
            (order_id,),
        )
        self.assertIsNotNone(row, "No payment row for order")
        self.assertGreater(
            row["refunded_minor"], 0,
            "payments.refunded_minor was not updated after approved refund",
        )

    def test_ineligible_refund_populates_summary_no_payment_change(self):
        order_id = _get_order_id_for_customer(self.INELIGIBLE_CUSTOMER)
        self.assertIsNotNone(order_id, "No order for C000002")

        before_row = fetch_one(
            "SELECT refunded_minor FROM payments WHERE order_id = ?",
            (order_id,),
        )
        refunded_before = before_row["refunded_minor"] if before_row else 0

        ticket_id = self._register(
            _make_ticket(self.INELIGIBLE_CUSTOMER, "refund", order_id,
                         "i want a refund")
        )
        run_id, thread_id = _make_run(ticket_id)
        log_event(run_id, self.INELIGIBLE_CUSTOMER, "workflow_started", None)

        initial_state = {
            "run_id": run_id,
            "ticket_id": ticket_id,
            "customer_id": self.INELIGIBLE_CUSTOMER,
            "order_id": order_id,
            "customer_message": "i want a refund",
            "order": None,
            "payment": None,
            "return_record": None,
            "policy_passages": [],
            "eligibility_result": None,
            "proposal_id": None,
            "reviewer_decision": None,
            "execution_result": None,
            "error_code": None,
            "final_response": None,
        }

        _invoke_graph(refund_workflow_graph, initial_state, thread_id)

        self.assert_response_chain(ticket_id, run_id, expected_run_status="completed")

        after_row = fetch_one(
            "SELECT refunded_minor FROM payments WHERE order_id = ?",
            (order_id,),
        )
        refunded_after = after_row["refunded_minor"] if after_row else 0
        self.assertEqual(
            refunded_before, refunded_after,
            "payments.refunded_minor changed for an ineligible refund",
        )


class TestExchangeChain(ResponseChainTestCase):
    """
    Two exchange terminal states:
      1. Eligible + approved  -> completed, new order created
      2. Ineligible (outside window) -> completed, no new order
    """

    ELIGIBLE_CUSTOMER = "C000003"
    INELIGIBLE_CUSTOMER = "C000004"

    def _count_orders(self, customer_id: str) -> int:
        rows = fetch_all(
            "SELECT id FROM orders WHERE customer_id = ?", (customer_id,)
        )
        return len(rows)

    def test_eligible_exchange_populates_summary_and_creates_order(self):
        order_id = _get_order_id_for_customer(self.ELIGIBLE_CUSTOMER)
        self.assertIsNotNone(order_id, "No order for C000003")

        orders_before = self._count_orders(self.ELIGIBLE_CUSTOMER)

        ticket_id = self._register(
            _make_ticket(self.ELIGIBLE_CUSTOMER, "exchange", order_id,
                         "i want to exchange my shoes")
        )
        run_id, thread_id = _make_run(ticket_id)
        log_event(run_id, self.ELIGIBLE_CUSTOMER, "workflow_started", None)

        initial_state = {
            "run_id": run_id,
            "ticket_id": ticket_id,
            "customer_id": self.ELIGIBLE_CUSTOMER,
            "order_id": order_id,
            "customer_message": "i want to exchange my shoes",
            "order": None,
            "payment": None,
            "policy_passages": [],
            "eligibility_result": None,
            "selected_replacement": None,
            "proposal_id": None,
            "reviewer_decision": None,
            "error_code": None,
            "final_response": None,
        }

        # Reset order state in case a prior run mutated it (e.g. exchange_requested)
        execute(
            "UPDATE orders SET status = 'delivered' WHERE id = ? AND customer_id = ?",
            (order_id, self.ELIGIBLE_CUSTOMER),
        )
        cfg = {"configurable": {"thread_id": thread_id}}
        exchange_workflow_graph.invoke(initial_state, config=cfg)

        proposal_row = fetch_one(
            "SELECT id FROM action_proposals WHERE run_id = ? AND status = 'pending'",
            (run_id,),
        )
        self.assertIsNotNone(
            proposal_row,
            "No pending proposal for eligible exchange -- graph did not pause",
        )
        proposal_id = proposal_row["id"]

        now = datetime.now(timezone.utc).isoformat()
        execute(
            "UPDATE action_proposals SET status = 'approved' WHERE id = ?",
            (proposal_id,),
        )
        approval_row = fetch_one(
            "SELECT id FROM approvals WHERE proposal_id = ?", (proposal_id,)
        )
        if approval_row:
            execute(
                "UPDATE approvals SET decision = 'approved', decided_at = ? "
                "WHERE id = ?",
                (now, approval_row["id"]),
            )
        else:
            approval_id = f"apv_test_{uuid.uuid4().hex[:8]}"
            execute(
                """INSERT INTO approvals
                       (id, proposal_id, reviewer_id, decision, decided_at, created_at)
                   VALUES (?, ?, 'R000001', 'approved', ?, ?)""",
                (approval_id, proposal_id, now, now),
            )

        exchange_workflow_graph.invoke(
            Command(resume={"decision": "approved", "proposal_id": proposal_id}),
            config=cfg,
        )

        self.assert_response_chain(ticket_id, run_id)

        orders_after = self._count_orders(self.ELIGIBLE_CUSTOMER)
        self.assertGreater(
            orders_after, orders_before,
            "No new order was created after approved exchange",
        )

    def test_ineligible_exchange_populates_summary_no_new_order(self):
        order_id = _get_order_id_for_customer(self.INELIGIBLE_CUSTOMER)
        self.assertIsNotNone(order_id, "No order for C000004")

        orders_before = self._count_orders(self.INELIGIBLE_CUSTOMER)

        ticket_id = self._register(
            _make_ticket(self.INELIGIBLE_CUSTOMER, "exchange", order_id,
                         "i want to exchange my shoes")
        )
        run_id, thread_id = _make_run(ticket_id)
        log_event(run_id, self.INELIGIBLE_CUSTOMER, "workflow_started", None)

        initial_state = {
            "run_id": run_id,
            "ticket_id": ticket_id,
            "customer_id": self.INELIGIBLE_CUSTOMER,
            "order_id": order_id,
            "customer_message": "i want to exchange my shoes",
            "order": None,
            "payment": None,
            "policy_passages": [],
            "eligibility_result": None,
            "selected_replacement": None,
            "proposal_id": None,
            "reviewer_decision": None,
            "error_code": None,
            "final_response": None,
        }

        _invoke_graph(exchange_workflow_graph, initial_state, thread_id)

        self.assert_response_chain(ticket_id, run_id, expected_run_status="completed")

        orders_after = self._count_orders(self.INELIGIBLE_CUSTOMER)
        self.assertEqual(
            orders_before, orders_after,
            "A new order was created for an ineligible exchange",
        )


class TestOrderPlacementChain(ResponseChainTestCase):
    """C000010 -- clean customer. Order placement pauses for confirmation."""

    CUSTOMER = "C000010"

    def test_order_placement_populates_summary_after_confirmation(self):
        ticket_id = self._register(
            _make_ticket(self.CUSTOMER, "order_placement", None,
                         "i want to buy wireless headphones")
        )
        run_id, thread_id = _make_run(ticket_id)
        log_event(run_id, self.CUSTOMER, "workflow_started", None)

        initial_state = {
            "run_id": run_id,
            "ticket_id": ticket_id,
            "customer_id": self.CUSTOMER,
            "order_id": None,
            "customer_message": "i want to buy wireless headphones",
            "search_results": [],
            "offered_product_ids": [],
            "options_message": None,
            "validated_product": None,
            "validated_products": None,
            "cart_item": None,
            "cart_items": None,
            "needs_clarification": False,
            "error_code": None,
            "final_response": None,
        }

        cfg = {"configurable": {"thread_id": thread_id}}

        # Phase 1: present_options_node expects CatalogSearchOutput
        self._gemini_mocks["order_placement"].return_value = json.dumps({
            "products": [
                {
                    "product_id": "prod_test_e1",
                    "name": "Sony WH-1000XM5 Headphones",
                    "category": "Electronics",
                    "price_minor": 2999900,
                    "price_display": "29,999"
                }
            ],
            "message": "Here are the wireless headphones we have available."
        })
        result = order_placement_graph.invoke(initial_state, config=cfg)
        run_status = _get_run_status(run_id)

        if run_status == "paused":
            # Phase 2: validate_selection_node expects OrderConfirmationOutput
            self._gemini_mocks["order_placement"].return_value = json.dumps({
                "selected_product_id": "prod_test_e1",
                "quantity": 1,
                "clarification_message": "",
                "needs_clarification": False
            })
            order_placement_graph.invoke(
                Command(resume="I'll take the first option"),
                config=cfg,
            )
            self.assert_response_chain(ticket_id, run_id, expected_run_status="completed")
        else:
            self.assert_response_chain(ticket_id, run_id)