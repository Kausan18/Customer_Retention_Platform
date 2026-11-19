import hashlib
import importlib.util
import json
import sys
from types import ModuleType

from fastapi.testclient import TestClient

if importlib.util.find_spec("sentence_transformers") is None:
    sentence_transformers = ModuleType("sentence_transformers")
    sentence_transformers.SentenceTransformer = type("SentenceTransformer", (), {})
    sys.modules["sentence_transformers"] = sentence_transformers

from app.api import approvals
from app.auth.session import _clear_all_sessions_for_testing, create_session, get_customer_id
from app.main import app


def setup_function():
    _clear_all_sessions_for_testing()


def teardown_function():
    _clear_all_sessions_for_testing()


def test_customer_and_reviewer_login_use_users_and_store_roles(monkeypatch):
    def fetch_user(query, params):
        assert "FROM users" in query
        if params == ("C000001", "1234"):
            return {
                "id": "usr_customer",
                "customer_id": "C000001",
                "name": "Aarav Sharma",
                "role": "customer",
            }
        if params == ("R000001", "9999"):
            return {
                "id": "usr_reviewer",
                "customer_id": "R000001",
                "name": "Reviewer One",
                "role": "reviewer",
            }
        return None

    monkeypatch.setattr("app.main.fetch_one", fetch_user)
    client = TestClient(app)

    customer_response = client.post(
        "/auth/login", json={"customer_id": "c000001", "pin": "1234"}
    )
    assert customer_response.status_code == 200
    customer_data = customer_response.json()
    assert customer_data["name"] == "Aarav Sharma"
    assert get_customer_id(customer_data["token"]) == {
        "customer_id": "C000001",
        "role": "customer",
    }

    reviewer_response = client.post(
        "/auth/reviewer-login",
        json={"customer_id": "R000001", "pin": "9999"},
    )
    assert reviewer_response.status_code == 200
    reviewer_data = reviewer_response.json()
    assert reviewer_data["reviewer_id"] == "R000001"
    assert reviewer_data["name"] == "Reviewer One"
    assert get_customer_id(reviewer_data["token"]) == {
        "customer_id": "R000001",
        "role": "reviewer",
    }

    customer_login_with_reviewer = client.post(
        "/auth/login", json={"customer_id": "R000001", "pin": "9999"}
    )
    assert customer_login_with_reviewer.status_code == 403
    assert customer_login_with_reviewer.json()["detail"] == (
        "Use the reviewer login endpoint"
    )


def test_reviewer_approval_routes_require_reviewer_session(monkeypatch):
    monkeypatch.setattr(approvals, "fetch_all", lambda *_args: [])
    client = TestClient(app)
    customer_token = create_session("C000001")
    reviewer_token = create_session("R000001", role="reviewer")

    assert client.get("/approvals").status_code == 401
    assert client.get(
        "/approvals", headers={"Authorization": f"Bearer {customer_token}"}
    ).status_code == 403
    assert client.get(
        "/approvals", headers={"X-Reviewer-Pin": "9999"}
    ).status_code == 401
    reviewer_response = client.get(
        "/approvals", headers={"Authorization": f"Bearer {reviewer_token}"}
    )
    assert reviewer_response.status_code == 200
    assert reviewer_response.json() == []


def test_approval_decision_uses_reviewer_session_identity(monkeypatch):
    payload = json.dumps({"amount_minor": 12500})
    proposal = {
        "id": "prp_test",
        "run_id": "run_test",
        "action_type": "refund",
        "status": "pending",
        "payload": payload,
        "payload_hash": hashlib.sha256(payload.encode()).hexdigest(),
        "expires_at": "2099-01-01T00:00:00Z",
    }
    approval = {"id": "apr_test", "proposal_id": "prp_test", "decision": None}
    fetch_results = iter([approval, proposal, {"status": "completed"}])
    monkeypatch.setattr(approvals, "fetch_one", lambda *_args: next(fetch_results))
    execute_calls = []
    monkeypatch.setattr(
        approvals, "execute", lambda query, params: execute_calls.append((query, params))
    )
    monkeypatch.setattr(approvals.audit, "log_event", lambda **_kwargs: None)
    monkeypatch.setattr(approvals, "_resume_workflow", lambda *_args: None)

    token = create_session("R000001", role="reviewer")
    response = TestClient(app).post(
        "/approvals/apr_test/decision",
        json={"decision": "approved", "reviewer_id": "forged_reviewer"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["reviewer_id"] == "R000001"
    approval_update = next(
        params for query, params in execute_calls if "UPDATE approvals" in query
    )
    assert approval_update[0] == "R000001"
