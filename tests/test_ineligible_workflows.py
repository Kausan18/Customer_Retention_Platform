from unittest.mock import patch

import pytest

from app.agents import exchange_workflow, refund_workflow


@pytest.mark.parametrize(
    ("error_code", "message"),
    [
        (
            "NOT_DELIVERED",
            "Your order hasn't been delivered yet. Refunds are only available after delivery.",
        ),
        (
            "OUTSIDE_WINDOW",
            "Your order is outside the 30-day refund window. "
            "Refunds must be requested within 30 days of delivery.",
        ),
        (
            "PAYMENT_NOT_PAID",
            "We don't have a completed payment on record for this order.",
        ),
        ("ALREADY_REFUNDED", "This order has already been fully refunded."),
        (
            "NO_RETURN_FOUND",
            "We haven't received a return request for this order yet. "
            "Please initiate a return first.",
        ),
        (
            "RETURN_NOT_RECEIVED",
            "Your return is still in transit. We'll process the refund once we receive it.",
        ),
        (
            "INSPECTION_NOT_PASSED",
            "Unfortunately your return did not pass inspection. "
            "Please contact support for next steps.",
        ),
        ("ORDER_NOT_FOUND", "We couldn't find that order on your account."),
    ],
)
def test_refund_ineligibility_returns_customer_message_and_resolves_ticket(
    error_code, message
):
    explained = refund_workflow.explain_ineligible_node(
        {"eligibility_result": {"eligible": False, "error_code": error_code}}
    )

    assert explained["final_response"] == message
    assert explained["run_status"] == "completed"
    assert explained["error_code"] is None

    state = {
        **explained,
        "run_id": "run_refund",
        "ticket_id": "ticket_refund",
        "customer_id": "customer_1",
    }
    with patch.object(refund_workflow, "update_ticket") as update_ticket, patch.object(
        refund_workflow, "log_event"
    ):
        refund_workflow.update_ticket_node(state)

    update_ticket.assert_called_once_with(
        "ticket_refund", "resolved", message, "customer_1"
    )


@pytest.mark.parametrize(
    ("error_code", "message"),
    [
        (
            "NOT_DELIVERED",
            "Your order hasn't been delivered yet. Exchanges are only available after delivery.",
        ),
        ("OUTSIDE_WINDOW", "Your order is outside the 15-day exchange window."),
        (
            "PAYMENT_NOT_PAID",
            "We don't have a completed payment on record for this order.",
        ),
        (
            "ALREADY_REFUNDED",
            "This order has already been refunded and is not eligible for exchange.",
        ),
        (
            "NO_SAME_CATEGORY_ITEM",
            "Exchanges are only available within the same product category.",
        ),
    ],
)
def test_exchange_ineligibility_returns_customer_message_and_resolves_ticket(
    error_code, message
):
    state = (
        {"error_code": error_code}
        if error_code == "NO_SAME_CATEGORY_ITEM"
        else {"eligibility_result": {"eligible": False, "error_code": error_code}}
    )
    explained = exchange_workflow.explain_ineligible_node(
        state
    )

    assert explained["final_response"] == message
    assert explained["run_status"] == "completed"
    assert explained["error_code"] is None

    state = {
        **explained,
        "run_id": "run_exchange",
        "ticket_id": "ticket_exchange",
        "customer_id": "customer_1",
    }
    with patch.object(exchange_workflow, "update_ticket") as update_ticket, patch.object(
        exchange_workflow, "log_event"
    ):
        exchange_workflow.update_ticket_node(state)

    update_ticket.assert_called_once_with(
        "ticket_exchange", "resolved", message, "customer_1"
    )


def test_ineligible_results_route_to_explanations():
    refund_state = {
        "eligibility_result": {"eligible": False, "error_code": "NOT_DELIVERED"}
    }
    exchange_state = {
        "eligibility_result": {"eligible": False, "error_code": "NOT_DELIVERED"}
    }

    assert refund_workflow._route_after_eligibility(refund_state) == "explain_ineligible"
    assert exchange_workflow._route_after_eligibility(exchange_state) == "explain_ineligible"


def test_eligibility_routes_true_false_and_missing_results():
    assert refund_workflow._route_after_eligibility(
        {"eligibility_result": {"eligible": True}}
    ) == "create_proposal"
    assert exchange_workflow._route_after_eligibility(
        {"eligibility_result": {"eligible": True}}
    ) == "search_catalog_node"

    for result in (None, {}, {"eligible": None}, "invalid"):
        state = {"eligibility_result": result}
        assert refund_workflow._route_after_eligibility(state) == "handle_error"
        assert exchange_workflow._route_after_eligibility(state) == "handle_error"


def test_refund_revalidation_ineligibility_routes_to_explanation():
    eligibility_result = {
        "eligible": False,
        "error_code": "RETURN_NOT_RECEIVED",
    }
    with patch.object(
        refund_workflow,
        "check_refund_eligibility",
        return_value=eligibility_result,
    ):
        result = refund_workflow.revalidate_node(
            {
                "order_id": "order_1",
                "reviewer_decision": {"decision": "approved"},
            }
        )

    assert result["eligibility_result"] == eligibility_result
    assert result["error_code"] is None
    assert refund_workflow._route_after_revalidate(result) == "explain_ineligible"


def test_exchange_revalidation_ineligibility_routes_to_explanation():
    eligibility_result = {
        "eligible": False,
        "error_code": "OUTSIDE_WINDOW",
    }
    with patch.object(
        exchange_workflow,
        "check_exchange_eligibility",
        return_value=eligibility_result,
    ):
        result = exchange_workflow.revalidate_node(
            {
                "order_id": "order_1",
                "reviewer_decision": {"decision": "approved"},
            }
        )

    assert result["eligibility_result"] == eligibility_result
    assert result["error_code"] is None
    assert exchange_workflow._route_after_revalidate(result) == "explain_ineligible"


def test_missing_refund_order_routes_to_ineligibility_explanation():
    with patch.object(
        refund_workflow,
        "get_recent_orders",
        return_value={"success": True, "orders": []},
    ):
        result = refund_workflow.investigate_node(
            {"customer_id": "customer_1", "order_id": None}
        )

    assert result["eligibility_result"]["error_code"] == "ORDER_NOT_FOUND"
    assert refund_workflow._route_after_investigate(result) == "explain_ineligible"
