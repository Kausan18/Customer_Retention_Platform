import importlib
import importlib.util
import inspect
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient


if importlib.util.find_spec("sentence_transformers") is None:
    sentence_transformers = ModuleType("sentence_transformers")
    sentence_transformers.SentenceTransformer = type("SentenceTransformer", (), {})
    sys.modules["sentence_transformers"] = sentence_transformers


def _import_app_module():
    from starlette.routing import Router

    original_init = Router.__init__
    if "on_startup" in inspect.signature(original_init).parameters:
        return importlib.import_module("app.main")

    def compatible_init(self, *args, **kwargs):
        on_startup = kwargs.pop("on_startup", None) or []
        on_shutdown = kwargs.pop("on_shutdown", None) or []
        original_init(self, *args, **kwargs)
        self.on_startup = on_startup
        self.on_shutdown = on_shutdown

    # Keep app import compatible with the FastAPI/Starlette versions in this environment.
    with patch.object(Router, "__init__", compatible_init):
        return importlib.import_module("app.main")


main = _import_app_module()
app = main.app
from app.api import runs, tickets
from app.auth.session import _clear_all_sessions_for_testing
from app.guardrails.input_screener import GuardrailDecision
from frontend.utils import session


class FastAPITestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._max_body_size_patch = None
        if not hasattr(app, "max_body_size"):
            cls._max_body_size_patch = patch.object(
                FastAPI,
                "max_body_size",
                None,
                create=True,
            )
            cls._max_body_size_patch.start()

    @classmethod
    def tearDownClass(cls):
        if cls._max_body_size_patch is not None:
            cls._max_body_size_patch.stop()
        super().tearDownClass()


class PolicyCategoryTests(FastAPITestCase):
    def setUp(self):
        _clear_all_sessions_for_testing()
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.login_lookup_patch = patch.object(
            main,
            "fetch_one",
            return_value={
                "id": "usr_test_customer",
                "customer_id": "C000001",
                "name": "Test Customer",
                "role": "customer",
            },
        )
        self.login_lookup_patch.start()
        self.addCleanup(self.login_lookup_patch.stop)

        login_response = self.client.post(
            "/auth/login",
            json={"customer_id": "C000001", "pin": "1234"},
        )
        self.assertEqual(login_response.status_code, 200)
        self.headers = {
            "Authorization": f"Bearer {login_response.json()['token']}"
        }

    def tearDown(self):
        _clear_all_sessions_for_testing()

    def _create_policy_ticket(self):
        with patch.object(
            tickets,
            "inspect_request",
            return_value=GuardrailDecision("ALLOW", "what is the refund policy", "OK"),
        ), patch.object(
            tickets.rate_limiter,
            "check",
            return_value=True,
        ), patch.object(tickets, "execute"), patch.object(
            tickets.audit, "log_event"
        ):
            return self.client.post(
                "/tickets",
                headers=self.headers,
                json={
                    "category": "policy",
                    "message": "what is the refund policy",
                },
            )

    def test_policy_category_accepted(self):
        response = self._create_policy_ticket()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["category"], "policy")

    def test_policy_run_starts(self):
        ticket_response = self._create_policy_ticket()
        self.assertEqual(ticket_response.status_code, 201)
        ticket_id = ticket_response.json()["ticket_id"]
        ticket = {
            "id": ticket_id,
            "customer_id": "C000001",
            "order_id": None,
            "category": "policy",
            "status": "open",
            "summary": "what is the refund policy",
        }

        with patch.object(
            tickets,
            "fetch_one",
            side_effect=[
                ticket,
                {"status": "completed", "completed_at": "2026-11-19T00:00:00Z"},
            ],
        ), patch.object(tickets, "execute"), patch.object(
            tickets.audit, "log_event"
        ), patch.object(
            tickets,
            "run_workflow",
            return_value={
                "run_status": "completed",
                "final_response": "Grounded policy response.",
            },
        ):
            response = self.client.post(
                f"/tickets/{ticket_id}/runs",
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 201)
        self.assertIn("run_id", response.json())


class PolicyRoutingTests(FastAPITestCase):
    def setUp(self):
        _clear_all_sessions_for_testing()
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        with patch.object(
            main,
            "fetch_one",
            return_value={
                "id": "usr_test_customer",
                "customer_id": "C000001",
                "name": "Test Customer",
                "role": "customer",
            },
        ):
            login_response = self.client.post(
                "/auth/login",
                json={"customer_id": "C000001", "pin": "1234"},
            )
        self.assertEqual(login_response.status_code, 200)
        self.headers = {
            "Authorization": f"Bearer {login_response.json()['token']}"
        }

    def tearDown(self):
        _clear_all_sessions_for_testing()

    def _run_policy_flow(self, question, final_response):
        ticket = {
            "id": "tkt_policy_test",
            "customer_id": "C000001",
            "order_id": None,
            "category": "policy",
            "status": "open",
            "summary": question,
        }
        retrieved_passage = {
            "document": "return_policy",
            "section": "Refund window",
            "content": "Refunds may be requested within 30 days of delivery.",
        }

        with patch.object(
            tickets,
            "inspect_request",
            return_value=GuardrailDecision("ALLOW", question, "OK"),
        ), patch.object(
            tickets.rate_limiter,
            "check",
            return_value=True,
        ), patch.object(
            tickets,
            "execute",
        ), patch.object(
            tickets.audit,
            "log_event",
        ), patch.object(
            tickets,
            "fetch_one",
            side_effect=[
                ticket,
                {"status": "completed", "completed_at": "2026-11-19T00:00:00Z"},
            ],
        ), patch.object(
            main,
            "search_policy_tool",
            return_value={"success": True, "passages": [retrieved_passage]},
        ), patch.object(
            main,
            "call_gemini",
            return_value=final_response,
        ) as call_gemini, patch.object(runs, "fetch_one") as run_lookup:
            ticket_response = self.client.post(
                "/tickets",
                headers=self.headers,
                json={"category": "policy", "message": question},
            )
            self.assertEqual(ticket_response.status_code, 201)
            ticket_id = ticket_response.json()["ticket_id"]
            ticket["id"] = ticket_id

            run_response = self.client.post(
                f"/tickets/{ticket_id}/runs",
                headers=self.headers,
            )
            self.assertEqual(run_response.status_code, 201)
            run_data = run_response.json()
            run_lookup.return_value = {
                "id": run_data["run_id"],
                "ticket_id": ticket_id,
                "thread_id": run_data["thread_id"],
                "status": "completed",
                "started_at": run_data["started_at"],
                "completed_at": "2026-11-19T00:00:00Z",
                "final_response": final_response,
                "ticket_customer_id": "C000001",
            }

            response = self.client.get(
                f"/runs/{run_data['run_id']}",
                headers=self.headers,
            )

        self.assertIn(question, call_gemini.call_args.args[0])
        self.assertIn(retrieved_passage["content"], call_gemini.call_args.args[0])
        return response.json()

    def test_policy_ticket_resolves_without_order(self):
        result = self._run_policy_flow(
            "what is the return policy",
            "Returns are accepted within 30 days of delivery under the policy.",
        )

        self.assertEqual(result["status"], "completed")
        self.assertIsNotNone(result["final_response"])
        self.assertGreater(len(result["final_response"]), 20)
        self.assertNotIn("encountered an issue", result["final_response"].lower())

    def test_policy_response_is_grounded(self):
        result = self._run_policy_flow(
            "what is the refund window",
            "Refunds may be requested within 30 days of delivery.",
        )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(
            "30" in result["final_response"] or "days" in result["final_response"]
        )


class SignupPersistenceTests(FastAPITestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "signup.sqlite3"
        connection = sqlite3.connect(self.database_path)
        try:
            connection.executescript(
                """
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    phone TEXT NOT NULL,
                    pin TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE addresses (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    line1 TEXT NOT NULL,
                    line2 TEXT NOT NULL,
                    city TEXT NOT NULL,
                    state TEXT NOT NULL,
                    pincode TEXT NOT NULL,
                    is_default INTEGER NOT NULL
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

        self.connection_patch = patch.object(main, "get_connection", self._get_connection)
        self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.email = f"signup-{uuid4().hex}@example.com"

    @contextmanager
    def _get_connection(self):
        connection = sqlite3.connect(self.database_path)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _signup(self):
        return self.client.post(
            "/auth/signup",
            json={
                "name": "Test Customer",
                "email": self.email,
                "phone": "",
                "pin": "1234",
                "address_line1": "1 Main Street",
                "address_line2": "",
                "city": "Example City",
                "state": "Example State",
                "pincode": "123456",
            },
        )

    def test_signup_success_message_contains_customer_id(self):
        response = self._signup()

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertIn("C", result["message"])
        self.assertTrue(result["customer_id"].startswith("C"))

    def test_duplicate_signup_returns_409(self):
        first_response = self._signup()
        second_response = self._signup()

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 409)


class ChatHistoryTests(unittest.TestCase):
    def setUp(self):
        self.session_state = {}
        self.session_state_patch = patch.object(
            session.st,
            "session_state",
            self.session_state,
        )
        self.session_state_patch.start()
        self.addCleanup(self.session_state_patch.stop)

    def test_save_session_to_history_ignores_empty_messages(self):
        self.session_state[session.MESSAGES] = []

        session.save_current_session_to_history()

        self.assertTrue(
            session.CHAT_HISTORY not in self.session_state
            or len(self.session_state[session.CHAT_HISTORY]) == 0
        )

    def test_save_session_to_history_persists_messages(self):
        messages = [
            {"role": "user", "content": "What is the return policy?"},
            {"role": "assistant", "content": "Returns are accepted within 30 days."},
        ]
        self.session_state[session.MESSAGES] = messages

        session.save_current_session_to_history()

        history = session.get_chat_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(
            history[0]["preview"],
            messages[0]["content"][:40],
        )
        self.assertEqual(history[0]["messages"], messages)

    def test_history_capped_at_10(self):
        for index in range(12):
            self.session_state[session.MESSAGES] = [
                {"role": "user", "content": f"Message {index}"}
            ]
            session.save_current_session_to_history()

        history = session.get_chat_history()
        self.assertEqual(len(history), 10)
        self.assertEqual(history[0]["preview"], "Message 2")
        self.assertEqual(history[-1]["preview"], "Message 11")

    def test_load_session_restores_messages(self):
        original_messages = [
            {"role": "user", "content": "What is the refund window?"},
            {"role": "assistant", "content": "Refunds are available within 30 days."},
        ]
        self.session_state[session.MESSAGES] = original_messages.copy()
        session.save_current_session_to_history()
        self.session_state[session.MESSAGES] = []

        session.init_new_chat_session()
        session.load_session_from_history(0)

        self.assertEqual(session.get_messages(), original_messages)
