import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from app.db.database import execute, fetch_all
from frontend.pages import chat
from frontend.utils import session


BASE_URL = "http://localhost:8080"
CUSTOMER_ID = "C000001"
CUSTOMER_PIN = "1234"


@pytest.fixture
def customer_session():
    login_response = requests.post(
        f"{BASE_URL}/auth/login",
        json={"customer_id": CUSTOMER_ID, "pin": CUSTOMER_PIN},
        timeout=10,
    )
    assert login_response.status_code == 200, login_response.text

    token = login_response.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    created_ticket_ids = []
    yield token, headers, created_ticket_ids

    for ticket_id in created_ticket_ids:
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

    requests.post(
        f"{BASE_URL}/auth/logout",
        headers=headers,
        timeout=10,
    )


def _poll_run(headers, run_id, expected_status, attempts=90):
    for _ in range(attempts):
        response = requests.get(
            f"{BASE_URL}/runs/{run_id}",
            headers=headers,
            timeout=10,
        )
        assert response.status_code == 200, response.text
        run = response.json()
        if run["status"] in {"completed", "failed", "paused"}:
            assert run["status"] == expected_status, run
            return run
        time.sleep(1)
    pytest.fail(f"Run {run_id} did not reach {expected_status}.")


def test_second_item_reuses_existing_ticket(customer_session):
    _, headers, created_ticket_ids = customer_session
    ticket_response = requests.post(
        f"{BASE_URL}/tickets",
        headers=headers,
        json={
            "category": "order_placement",
            "message": "I want wireless headphones",
        },
        timeout=10,
    )
    assert ticket_response.status_code == 201, ticket_response.text
    ticket_id = ticket_response.json()["ticket_id"]
    created_ticket_ids.append(ticket_id)

    first_run_response = requests.post(
        f"{BASE_URL}/tickets/{ticket_id}/runs",
        headers=headers,
        timeout=60,
    )
    assert first_run_response.status_code == 201, first_run_response.text
    first_run_id = first_run_response.json()["run_id"]
    _poll_run(headers, first_run_id, "paused")

    selection_response = requests.post(
        f"{BASE_URL}/runs/{first_run_id}/messages",
        headers=headers,
        json={"message": "I'll take the first option"},
        timeout=60,
    )
    assert selection_response.status_code == 200, selection_response.text
    assert selection_response.json()["status"] == "completed"

    completed_run = _poll_run(headers, first_run_id, "completed")
    assert completed_run.get("cart_item") or any(
        phrase in (completed_run.get("final_response") or "").lower()
        for phrase in ("added", "cart")
    )

    second_run_response = requests.post(
        f"{BASE_URL}/tickets/{ticket_id}/runs",
        headers=headers,
        json={"message": "show me bluetooth speakers"},
        timeout=60,
    )
    assert second_run_response.status_code == 201, second_run_response.text
    assert second_run_response.json()["ticket_id"] == ticket_id


def test_new_ticket_created_when_cart_empty(customer_session, monkeypatch):
    token, _, created_ticket_ids = customer_session
    ui = SimpleNamespace(session_state={})
    monkeypatch.setattr(chat, "st", ui)
    monkeypatch.setattr(session, "st", ui)
    monkeypatch.setattr(chat.api, "BASE_URL", BASE_URL)
    create_ticket = chat.api.create_ticket

    def create_ticket_and_track(*args, **kwargs):
        result = create_ticket(*args, **kwargs)
        if result.get("success"):
            created_ticket_ids.append(result["ticket_id"])
        return result

    monkeypatch.setattr(chat.api, "create_ticket", create_ticket_and_track)
    monkeypatch.setattr(
        chat.api,
        "start_run",
        Mock(return_value={"success": True, "run_id": "test-run"}),
    )
    monkeypatch.setattr(
        chat.api,
        "poll_run",
        Mock(
            return_value={
                "success": True,
                "status": "paused",
                "final_response": "Which speaker would you like?",
            }
        ),
    )
    session.set_user(token, CUSTOMER_ID, "customer", "Customer")
    ui.session_state.update(
        {
            "cart_items": [],
            "current_category": "order_placement",
            "ticket_id": "previous-ticket",
            session.CURRENT_TICKET_ID: "previous-ticket",
            session.AWAITING_CONFIRMATION: False,
        }
    )

    chat._handle_message("show me bluetooth speakers")

    new_ticket_id = ui.session_state[session.CURRENT_TICKET_ID]
    assert new_ticket_id != "previous-ticket"
    assert ui.session_state["current_category"] == "order_placement"


def test_cart_cleared_on_new_chat():
    state = {
        "cart_items": [
            {"product_id": "prod_001", "name": "Headphones", "quantity": 1}
        ]
    }

    with patch.object(session, "st", SimpleNamespace(session_state=state)):
        session.init_new_chat_session()

    assert state["cart_items"] == []
