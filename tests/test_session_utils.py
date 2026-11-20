import unittest
from unittest.mock import patch

from frontend.utils import session


class SessionUtilsTests(unittest.TestCase):
    def setUp(self):
        self.session_state = {}
        self.session_state_patch = patch.object(
            session.st,
            "session_state",
            self.session_state,
        )
        self.session_state_patch.start()
        self.addCleanup(self.session_state_patch.stop)

    def test_new_chat_clears_all_chat_keys(self):
        self.session_state.update(
            {
                "messages": ["hi"],
                "ticket_id": "tkt_abc",
                "run_id": "run_abc",
                "awaiting_confirmation": True,
                "cart_items": [{"id": "prod_001"}],
                "current_category": "refund",
                "approval_pending": True,
                "approval_id": "apr_abc",
                "confirmed_order": {"order_id": "ord_abc"},
                "last_user_message": "hello",
                "pending_message": "world",
                "customer_id": "C000001",
                "token": "tok_abc",
                "page": "chat",
                "logged_in": True,
            }
        )

        session.init_new_chat_session()

        self.assertEqual(self.session_state["messages"], [])
        self.assertIsNone(self.session_state["ticket_id"])
        self.assertIsNone(self.session_state["run_id"])
        self.assertFalse(self.session_state["awaiting_confirmation"])
        self.assertEqual(self.session_state["cart_items"], [])
        self.assertIsNone(self.session_state["current_category"])
        self.assertFalse(self.session_state["approval_pending"])
        self.assertIsNone(self.session_state["approval_id"])
        self.assertIsNone(self.session_state["confirmed_order"])
        self.assertIsNone(self.session_state["last_user_message"])
        self.assertIsNone(self.session_state["pending_message"])
        self.assertEqual(self.session_state["customer_id"], "C000001")
        self.assertEqual(self.session_state["token"], "tok_abc")
        self.assertEqual(self.session_state["page"], "chat")
        self.assertTrue(self.session_state["logged_in"])

    def test_init_session_state_sets_defaults(self):
        self.session_state.clear()

        session.init_session_state()

        expected_defaults = {
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
        for key in session.CHAT_SESSION_KEYS:
            self.assertEqual(self.session_state[key], expected_defaults[key])
        self.assertEqual(self.session_state["messages"], [])


if __name__ == "__main__":
    unittest.main()
