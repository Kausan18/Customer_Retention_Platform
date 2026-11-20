import time

import pytest
import requests

from app.db.database import execute, fetch_all, fetch_one


BASE_URL = "http://localhost:8080"
CUSTOMER_PIN = "1234"


@pytest.fixture
def cleanup_api_data():
    ticket_ids = []
    tokens = []
    yield ticket_ids, tokens

    for ticket_id in ticket_ids:
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

    for token in tokens:
        requests.post(
            f"{BASE_URL}/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )


def _login(customer_id, cleanup_api_data):
    response = requests.post(
        f"{BASE_URL}/auth/login",
        json={"customer_id": customer_id, "pin": CUSTOMER_PIN},
        timeout=10,
    )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    cleanup_api_data[1].append(token)
    return {"Authorization": f"Bearer {token}"}


def _start_refund_run(headers, cleanup_api_data, message, order_id=None):
    body = {"category": "refund", "message": message}
    if order_id is not None:
        body["order_id"] = order_id
    ticket_response = requests.post(
        f"{BASE_URL}/tickets",
        headers=headers,
        json=body,
        timeout=10,
    )
    assert ticket_response.status_code == 201, ticket_response.text
    ticket_id = ticket_response.json()["ticket_id"]
    cleanup_api_data[0].append(ticket_id)

    run_response = requests.post(
        f"{BASE_URL}/tickets/{ticket_id}/runs",
        headers=headers,
        timeout=10,
    )
    assert run_response.status_code == 201, run_response.text
    return run_response.json()["run_id"]


def _poll_until_completed_or_failed(headers, run_id, timeout_seconds=15):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        response = requests.get(
            f"{BASE_URL}/runs/{run_id}",
            headers=headers,
            timeout=min(5, remaining),
        )
        assert response.status_code == 200, response.text
        run = response.json()
        if run.get("status") in {"completed", "failed"}:
            return run
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    pytest.fail(f"Run {run_id} did not complete or fail within {timeout_seconds}s.")


def test_ineligible_refund_returns_policy_message_not_error(cleanup_api_data):
    debug_response = requests.get(
        f"{BASE_URL}/debug/eligible-refund",
        timeout=10,
    )
    if debug_response.status_code == 404:
        pytest.skip("no eligible order")
    assert debug_response.status_code == 200, debug_response.text

    customer_id = debug_response.json()["customer_id"]
    headers = _login(customer_id, cleanup_api_data)
    run_id = _start_refund_run(
        headers,
        cleanup_api_data,
        message="I want a refund",
    )
    run = _poll_until_completed_or_failed(headers, run_id)

    assert run["status"] == "completed"
    final_response = run.get("final_response")
    assert final_response is not None
    assert "encountered an issue" not in final_response.lower()
    assert "error" not in final_response.lower()


def test_outside_window_refund_explains_policy(cleanup_api_data):
    order = fetch_one(
        """
        SELECT id AS order_id, customer_id
        FROM orders
        WHERE status = 'delivered'
          AND delivered_at IS NOT NULL
          AND datetime(delivered_at) < datetime('now', '-31 days')
        ORDER BY datetime(delivered_at), id
        LIMIT 1
        """
    )
    if order is None:
        pytest.skip("no delivered order older than 31 days")

    headers = _login(order["customer_id"], cleanup_api_data)
    run_id = _start_refund_run(
        headers,
        cleanup_api_data,
        message=f"I want a refund for {order['order_id']}",
        order_id=order["order_id"],
    )
    run = _poll_until_completed_or_failed(headers, run_id)

    assert run["status"] == "completed"
    final_response = (run.get("final_response") or "").lower()
    assert any(
        phrase in final_response
        for phrase in ("30", "not eligible", "window", "policy")
    )
