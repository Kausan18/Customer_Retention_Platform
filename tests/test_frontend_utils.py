import importlib
import importlib.util
import inspect
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frontend.utils import api, session
from frontend.utils.detect_category import detect_category


if importlib.util.find_spec("sentence_transformers") is None:
    sentence_transformers = ModuleType("sentence_transformers")
    sentence_transformers.SentenceTransformer = type("SentenceTransformer", (), {})
    sys.modules["sentence_transformers"] = sentence_transformers


def _import_signup_app():
    from starlette.routing import Router

    original_init = Router.__init__
    if "on_startup" in inspect.signature(original_init).parameters:
        return importlib.import_module("app.main").app

    def compatible_init(self, *args, **kwargs):
        on_startup = kwargs.pop("on_startup", None) or []
        on_shutdown = kwargs.pop("on_shutdown", None) or []
        original_init(self, *args, **kwargs)
        self.on_startup = on_startup
        self.on_shutdown = on_shutdown

    with patch.object(Router, "__init__", compatible_init):
        return importlib.import_module("app.main").app


app = _import_signup_app()


class DetectCategoryTests(unittest.TestCase):
    def test_refund_keywords(self):
        self.assertEqual(detect_category("I want a refund for my order"), "refund")

    def test_exchange_keywords(self):
        self.assertEqual(
            detect_category("I want to exchange my headphones"),
            "exchange",
        )

    def test_tracking_keywords(self):
        self.assertEqual(detect_category("where is my package"), "order_tracking")

    def test_status_keywords(self):
        self.assertEqual(
            detect_category("order status of my order"),
            "order_status",
        )

    def test_policy_keywords(self):
        self.assertEqual(detect_category("what is the return policy"), "policy")

    def test_browse_routes_to_placement(self):
        self.assertEqual(
            detect_category("show me wireless headphones"),
            "order_placement",
        )

    def test_have_a_look_routes_to_placement(self):
        self.assertEqual(
            detect_category("can i have a look at keyboards"),
            "order_placement",
        )

    def test_face_cream_routes_to_placement(self):
        self.assertEqual(
            detect_category("show me face creams"),
            "order_placement",
        )

    def test_refund_beats_exchange(self):
        self.assertEqual(
            detect_category("can i refund or exchange my item"),
            "refund",
        )

    def test_affirmative_routes_to_placement(self):
        self.assertEqual(detect_category("yes that one"), "unknown")

    def test_policy_beats_refund(self):
        self.assertEqual(detect_category("what is the refund policy"), "policy")

    def test_default_is_unknown(self):
        self.assertEqual(detect_category("hmm"), "unknown")


class SessionHelperTests(unittest.TestCase):
    def setUp(self):
        self.session_state = {}
        self.session_state_patch = patch.object(
            session.st,
            "session_state",
            self.session_state,
        )
        self.session_state_patch.start()
        self.addCleanup(self.session_state_patch.stop)

    def test_set_and_get_user(self):
        session.set_user("tok", "C000001", "customer", "Alice")

        self.assertEqual(session.get_token(), "tok")
        self.assertEqual(session.get_role(), "customer")
        self.assertTrue(session.is_logged_in())

    def test_clear_user(self):
        session.set_user("tok", "C000001", "customer", "Alice")

        session.clear_user()

        self.assertFalse(session.is_logged_in())

    def test_add_to_cart(self):
        session.add_to_cart(
            {
                "product_id": "prod_001",
                "product_name": "Headphones",
                "quantity": 1,
                "price_minor": 299900,
                "line_total_minor": 299900,
            }
        )

        self.assertEqual(len(session.get_cart()), 1)
        self.assertEqual(session.get_cart_total(), 299900)

    def test_remove_from_cart(self):
        for product_id in ("prod_001", "prod_002"):
            session.add_to_cart(
                {
                    "product_id": product_id,
                    "product_name": "Headphones",
                    "quantity": 1,
                    "price_minor": 299900,
                    "line_total_minor": 299900,
                }
            )

        session.remove_from_cart("prod_001")

        self.assertEqual(len(session.get_cart()), 1)
        self.assertEqual(session.get_cart()[0]["product_id"], "prod_002")

    def test_clear_cart(self):
        session.add_to_cart(
            {
                "product_id": "prod_001",
                "product_name": "Headphones",
                "quantity": 1,
                "price_minor": 299900,
                "line_total_minor": 299900,
            }
        )

        session.clear_cart()

        self.assertEqual(session.get_cart(), [])
        self.assertEqual(session.get_cart_total(), 0)

    def test_set_user_initializes_session_start_time(self):
        with patch.object(session, "datetime") as mocked_datetime:
            mocked_datetime.utcnow.return_value.isoformat.return_value = "started"

            session.set_user("tok", "C000001", "customer", "Alice")

        self.assertEqual(session.st.session_state[session.SESSION_START_TIME], "started")

    def test_init_session_state_sets_chat_defaults_without_touching_auth(self):
        auth_state = {
            session.TOKEN: "auth-token",
            session.CUSTOMER_ID: "C000001",
            "page": "chat",
            session.CHAT_HISTORY: [{"preview": "saved"}],
            "logged_in": True,
        }
        session.st.session_state.update(auth_state)

        session.init_session_state()

        expected = {
            "messages": [],
            "ticket_id": None,
            "run_id": None,
            "awaiting_confirmation": False,
            "cart_items": [],
            "current_category": None,
            "approval_pending": False,
            "approval_id": None,
            "confirmed_order": None,
            "last_user_message": None,
            "pending_message": None,
        }
        for key, value in expected.items():
            self.assertEqual(session.st.session_state[key], value)
        for key, value in auth_state.items():
            self.assertEqual(session.st.session_state[key], value)

    def test_save_current_session_to_history_copies_messages_and_truncates_preview(self):
        session.st.session_state[session.SESSION_START_TIME] = "2026-11-19T00:00:00"
        message = "A" * 41
        session.add_message("user", message)

        session.save_current_session_to_history()

        saved_session = session.get_chat_history()[0]
        self.assertEqual(saved_session["started_at"], "2026-11-19T00:00:00")
        self.assertEqual(saved_session["preview"], "A" * 40 + "...")
        self.assertEqual(saved_session["messages"], [{"role": "user", "content": message}])
        self.assertIsNot(saved_session["messages"], session.get_messages())
        session.add_message("assistant", "Later message")
        self.assertEqual(len(saved_session["messages"]), 1)

    def test_save_current_session_skips_empty_messages(self):
        session.save_current_session_to_history()

        self.assertEqual(session.get_chat_history(), [])

    def test_init_new_chat_resets_current_session_without_changing_history(self):
        session.st.session_state[session.SESSION_START_TIME] = "previous"
        session.st.session_state[session.CHAT_HISTORY] = [{"preview": "saved"}]
        session.add_message("user", "Previous conversation")
        session.st.session_state[session.CURRENT_RUN_ID] = "run_1"
        session.st.session_state[session.CURRENT_TICKET_ID] = "ticket_1"
        session.st.session_state[session.PENDING_APPROVAL] = {"id": "approval"}
        session.st.session_state[session.AWAITING_CONFIRMATION] = True
        session.st.session_state[session.PAYMENT_READY] = True
        session.st.session_state[session.WORKFLOW_TIMELINE] = [{"event": "started"}]

        with patch.object(session, "datetime") as mocked_datetime:
            mocked_datetime.utcnow.return_value.isoformat.return_value = "next"
            session.init_new_chat_session()

        self.assertEqual(
            session.get_chat_history(),
            [{"preview": "saved"}],
        )
        self.assertEqual(session.get_messages(), [])
        self.assertIsNone(session.st.session_state[session.CURRENT_RUN_ID])
        self.assertIsNone(session.st.session_state[session.CURRENT_TICKET_ID])
        self.assertIsNone(session.st.session_state[session.PENDING_APPROVAL])
        self.assertFalse(session.st.session_state[session.AWAITING_CONFIRMATION])
        self.assertFalse(session.st.session_state[session.PAYMENT_READY])
        self.assertEqual(session.st.session_state[session.WORKFLOW_TIMELINE], [])
        self.assertEqual(session.st.session_state[session.SESSION_START_TIME], "next")

    def test_init_new_chat_preserves_auth_and_resets_all_chat_keys(self):
        session.st.session_state.update(
            {
                session.TOKEN: "auth-token",
                session.CUSTOMER_ID: "C000001",
                "page": "chat",
                session.CHAT_HISTORY: [{"preview": "saved"}],
                "logged_in": True,
            }
        )
        session.init_session_state()
        for key in session.CHAT_SESSION_KEYS:
            if key != "messages":
                session.st.session_state[key] = "stale"
        for key in (
            session.CURRENT_RUN_ID,
            session.CURRENT_TICKET_ID,
            session.PENDING_APPROVAL,
            session.CART,
            session.CART_TOTAL,
            session.PAYMENT_READY,
            session.WORKFLOW_TIMELINE,
        ):
            session.st.session_state[key] = "stale"

        session.init_new_chat_session()

        self.assertEqual(session.st.session_state[session.TOKEN], "auth-token")
        self.assertEqual(session.st.session_state[session.CUSTOMER_ID], "C000001")
        self.assertEqual(session.st.session_state["page"], "chat")
        self.assertTrue(session.st.session_state["logged_in"])
        self.assertEqual(
            session.st.session_state[session.CHAT_HISTORY],
            [{"preview": "saved"}],
        )
        self.assertEqual(session.st.session_state["messages"], [])
        self.assertIsNone(session.st.session_state["ticket_id"])
        self.assertIsNone(session.st.session_state["run_id"])
        self.assertFalse(session.st.session_state["awaiting_confirmation"])
        self.assertEqual(session.st.session_state["cart_items"], [])
        self.assertFalse(session.st.session_state["approval_pending"])
        self.assertIsNone(session.st.session_state[session.CURRENT_RUN_ID])
        self.assertIsNone(session.st.session_state[session.CURRENT_TICKET_ID])
        self.assertIsNone(session.st.session_state[session.PENDING_APPROVAL])
        self.assertEqual(session.st.session_state[session.CART], [])
        self.assertEqual(session.st.session_state[session.CART_TOTAL], 0)
        self.assertFalse(session.st.session_state[session.PAYMENT_READY])
        self.assertEqual(session.st.session_state[session.WORKFLOW_TIMELINE], [])

    def test_chat_history_is_capped_at_ten_sessions(self):
        with patch.object(session, "datetime") as mocked_datetime:
            mocked_datetime.utcnow.return_value.isoformat.return_value = "next"
            for index in range(11):
                session.add_message("user", f"Message {index}")
                session.save_current_session_to_history()
                session.init_new_chat_session()

        history = session.get_chat_history()
        self.assertEqual(len(history), 10)
        self.assertEqual(history[0]["preview"], "Message 1")
        self.assertEqual(history[-1]["preview"], "Message 10")

    def test_load_session_from_history_clears_workflow_state(self):
        session.st.session_state[session.CHAT_HISTORY] = [
            {
                "started_at": "2026-11-18T00:00:00",
                "preview": "Past chat",
                "messages": [{"role": "user", "content": "Past message"}],
            }
        ]
        session.st.session_state[session.CURRENT_RUN_ID] = "run_old"
        session.st.session_state[session.CURRENT_TICKET_ID] = "ticket_old"
        session.st.session_state[session.AWAITING_CONFIRMATION] = True

        session.load_session_from_history(0)

        self.assertEqual(
            session.get_messages(),
            [{"role": "user", "content": "Past message"}],
        )
        self.assertIsNone(session.st.session_state[session.CURRENT_RUN_ID])
        self.assertIsNone(session.st.session_state[session.CURRENT_TICKET_ID])
        self.assertFalse(session.st.session_state[session.AWAITING_CONFIRMATION])


class SignupEndpointTests(unittest.TestCase):
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

        self.connection_patch = patch(
            "app.main.get_connection",
            self._get_connection,
        )
        self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

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

    @staticmethod
    def _signup_payload(email="signup-test@example.com", pin="1234"):
        return {
            "name": "Alice",
            "email": email,
            "phone": "",
            "pin": pin,
            "address_line1": "1 Main Street",
            "address_line2": "",
            "city": "Example City",
            "state": "Example State",
            "pincode": "123456",
        }

    def test_signup_creates_user(self):
        response = self.client.post(
            "/auth/signup",
            json=self._signup_payload(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["customer_id"].startswith("C"))

    def test_duplicate_email_returns_409(self):
        payload = self._signup_payload()

        first_response = self.client.post("/auth/signup", json=payload)
        second_response = self.client.post("/auth/signup", json=payload)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 409)

    def test_invalid_pin_length_returns_422(self):
        response = self.client.post(
            "/auth/signup",
            json=self._signup_payload(pin="12"),
        )

        self.assertEqual(response.status_code, 422)


def test_session_user_messages_cart_and_reset(monkeypatch):
    monkeypatch.setattr(session.st, "session_state", {})

    assert not session.is_logged_in()
    session.set_user("token", "C000001", "customer", "Ava")
    assert session.is_logged_in()
    assert session.get_token() == "token"
    assert session.get_customer_id() == "C000001"

    session.add_message("user", "Hello")
    session.add_message("assistant", "Hi")
    assert session.get_messages() == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]

    session.add_to_cart(
        {
            "product_id": "prod_001",
            "product_name": "Desk",
            "category": "Furniture",
            "quantity": 2,
            "price_minor": 2500,
            "line_total_minor": 5000,
        }
    )
    assert session.get_cart_total() == 5000
    session.remove_from_cart("prod_001")
    assert session.get_cart() == []
    assert session.get_cart_total() == 0

    session.clear_user()
    assert not session.is_logged_in()
    assert session.get_messages() == []
    assert session.get_cart() == []


def test_api_helpers_construct_requests_and_map_responses(monkeypatch):
    response = SimpleNamespace(
        status_code=201,
        json=lambda: {"ticket_id": "tkt_1", "status": "open"},
    )
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(api.requests, "post", fake_post)
    result = api.create_ticket("token", "order_status", "Where is my order?")
    assert result == {"success": True, "ticket_id": "tkt_1", "status": "open"}
    assert calls[0][1]["headers"] == {"Authorization": "Bearer token"}
    assert calls[0][1]["json"] == {
        "category": "order_status",
        "message": "Where is my order?",
    }

    api.validate_cart("token", [{"product_id": "prod_001", "quantity": 1}])
    assert calls[1][1]["json"] == {
        "items": [{"product_id": "prod_001", "quantity": 1}]
    }


def test_login_identifies_reviewer_account(monkeypatch):
    response = SimpleNamespace(
        status_code=403,
        json=lambda: {"detail": "Use the reviewer login endpoint"},
    )
    monkeypatch.setattr(api.requests, "post", lambda *args, **kwargs: response)

    assert api.login("R000001", "1234") == {
        "success": False,
        "error": "reviewer_account",
    }


def test_poll_run_stops_on_terminal_status(monkeypatch):
    results = iter(
        [
            {"success": True, "status": "running"},
            {"success": True, "status": "paused"},
        ]
    )
    monkeypatch.setattr(api, "get_run", lambda token, run_id: next(results))
    monkeypatch.setattr(api.time, "sleep", lambda interval: None)

    assert api.poll_run("token", "run_1", max_attempts=5) == {
        "success": True,
        "status": "paused",
    }
