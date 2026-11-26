from contextlib import nullcontext
from unittest.mock import Mock

from frontend.pages import chat
from frontend.utils import session


class FakeStreamlit:
    def __init__(self):
        self.session_state = {}


class FakeChatPageStreamlit(FakeStreamlit):
    def __init__(self, prompts):
        super().__init__()
        self.prompts = iter(prompts)
        self.events = []
        self.container_kwargs = []
        self.markdown_calls = []
        self.sidebar = nullcontext()

    def markdown(self, body, **kwargs):
        self.markdown_calls.append((body, kwargs))

    def columns(self, spec):
        return [nullcontext() for _ in spec]

    def header(self, *args, **kwargs):
        pass

    def button(self, *args, **kwargs):
        return False

    def container(self, **kwargs):
        self.container_kwargs.append(kwargs)
        return nullcontext()

    def chat_input(self, *args, **kwargs):
        self.events.append("chat_input")
        return next(self.prompts)

    def rerun(self):
        self.events.append("rerun")


class FakeSidebarStreamlit(FakeStreamlit):
    def __init__(self, clicked_button=None):
        super().__init__()
        self.clicked_button = clicked_button
        self.buttons = []
        self.reruns = 0

    def button(self, label, **kwargs):
        self.buttons.append((label, kwargs))
        return (
            label == self.clicked_button
            or kwargs.get("key") == self.clicked_button
        )

    def subheader(self, *args, **kwargs):
        pass

    def caption(self, *args, **kwargs):
        pass

    def write(self, *args, **kwargs):
        pass

    def divider(self):
        pass

    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def rerun(self):
        self.reruns += 1


def _set_session(monkeypatch):
    ui = FakeStreamlit()
    monkeypatch.setattr(chat, "st", ui)
    monkeypatch.setattr(session, "st", ui)
    return ui


def test_sidebar_new_chat_button_starts_a_new_session(monkeypatch):
    ui = FakeSidebarStreamlit(clicked_button="➕ New Chat")
    monkeypatch.setattr(chat, "st", ui)
    monkeypatch.setattr(session, "st", ui)
    init_new_chat = Mock()
    monkeypatch.setattr(session, "init_new_chat_session", init_new_chat)

    chat._render_sidebar()

    init_new_chat.assert_called_once_with()
    assert ui.reruns == 1


def test_sidebar_history_shows_newest_first_and_loads_selected_session(monkeypatch):
    ui = FakeSidebarStreamlit(clicked_button="history_1")
    ui.session_state[session.CHAT_HISTORY] = [
        {"preview": "Older chat", "started_at": "2026-11-18T10:00:00"},
        {"preview": "Newer chat", "started_at": "2026-11-19T10:00:00"},
    ]
    monkeypatch.setattr(chat, "st", ui)
    monkeypatch.setattr(session, "st", ui)
    load_session = Mock()
    monkeypatch.setattr(session, "load_session_from_history", load_session)

    chat._render_sidebar()

    history_buttons = [
        (label, options)
        for label, options in ui.buttons
        if options.get("key", "").startswith("history_")
    ]
    assert history_buttons == [
        ("Newer chat (2026-11-19)", {"key": "history_1", "use_container_width": True}),
    ]
    load_session.assert_called_once_with(1)
    assert ui.reruns == 1


def test_handle_message_creates_ticket_and_adds_completed_response(monkeypatch):
    ui = _set_session(monkeypatch)
    session.set_user("token", "C000001", "customer", "Ava")
    monkeypatch.setattr(chat, "classify_intent", lambda prompt: "order_status")
    monkeypatch.setattr(
        chat.api,
        "create_ticket",
        Mock(return_value={"success": True, "ticket_id": "ticket_1"}),
    )
    monkeypatch.setattr(
        chat.api,
        "start_run",
        Mock(return_value={"success": True, "run_id": "run_1"}),
    )
    monkeypatch.setattr(
        chat.api,
        "poll_run",
        Mock(
            return_value={
                "success": True,
                "status": "completed",
                "final_response": "Your order is confirmed.",
            }
        ),
    )

    chat._handle_message("Is my order confirmed?")

    assert ui.session_state[session.CURRENT_TICKET_ID] == "ticket_1"
    assert ui.session_state[session.CURRENT_RUN_ID] == "run_1"
    assert session.get_messages()[-1] == {
        "role": "assistant",
        "content": "Your order is confirmed.",
    }


def test_checkout_with_items_marks_payment_ready_without_workflow(monkeypatch):
    ui = _set_session(monkeypatch)
    ui.session_state[session.CART] = [
        {"line_total_minor": 1250, "product_id": "prod_1"}
    ]
    ui.session_state[session.CART_TOTAL] = 1250
    create_ticket = Mock()
    monkeypatch.setattr(chat.api, "create_ticket", create_ticket)
    start_run = Mock()
    monkeypatch.setattr(chat.api, "start_run", start_run)
    classify_intent = Mock()
    monkeypatch.setattr(chat, "classify_intent", classify_intent)

    chat._handle_message("That's all, checkout")

    assert session.is_payment_ready()
    assert session.get_messages()[-1]["content"] == (
        "Taking you to payment. ₹12.50 total."
    )
    create_ticket.assert_not_called()
    start_run.assert_not_called()
    classify_intent.assert_not_called()


def test_unknown_message_requests_clarification_without_ticket(monkeypatch):
    ui = _set_session(monkeypatch)
    session.set_user("token", "C000001", "customer", "Ava")
    create_ticket = Mock()
    start_run = Mock()
    monkeypatch.setattr(chat.api, "create_ticket", create_ticket)
    monkeypatch.setattr(chat.api, "start_run", start_run)
    monkeypatch.setattr(chat, "classify_intent", lambda prompt: "unknown")

    chat._handle_message("Can you help me?")

    create_ticket.assert_not_called()
    start_run.assert_not_called()
    assert "clarify" in session.get_messages()[-1]["content"].lower()


def test_damaged_item_creates_support_ticket_without_workflow(monkeypatch):
    ui = _set_session(monkeypatch)
    session.set_user("token", "C000001", "customer", "Ava")
    monkeypatch.setattr(chat.api, "create_ticket", Mock(
        return_value={"success": True, "ticket_id": "ticket_damaged"}
    ))
    start_run = Mock()
    monkeypatch.setattr(chat.api, "start_run", start_run)

    chat._handle_message("The headphones arrived broken")

    chat.api.create_ticket.assert_called_once_with(
        "token",
        "damaged",
        "The headphones arrived broken",
    )
    start_run.assert_not_called()
    assert ui.session_state[session.CURRENT_TICKET_ID] == "ticket_damaged"
    assert "support ticket" in session.get_messages()[-1]["content"].lower()


def test_completed_order_placement_adds_cart_item_from_run_detail(monkeypatch):
    ui = _set_session(monkeypatch)
    session.set_user("token", "C000001", "customer", "Ava")
    monkeypatch.setattr(chat, "classify_intent", lambda prompt: "order_placement")
    monkeypatch.setattr(
        chat.api,
        "create_ticket",
        Mock(return_value={"success": True, "ticket_id": "ticket_1"}),
    )
    monkeypatch.setattr(
        chat.api,
        "start_run",
        Mock(return_value={"success": True, "run_id": "run_1"}),
    )
    monkeypatch.setattr(
        chat.api,
        "poll_run",
        Mock(
            return_value={
                "success": True,
                "status": "completed",
                "final_response": "Added headphones to your cart.",
            }
        ),
    )
    get_run = Mock(
        return_value={
            "success": True,
            "cart_item": {
                "product_id": "prod_1",
                "product_name": "Headphones",
                "price_minor": 2500,
                "category": "Audio",
                "quantity": 2,
            },
        }
    )
    monkeypatch.setattr(chat.api, "get_run", get_run)

    chat._handle_message("Add headphones")

    get_run.assert_called_once_with("token", "run_1")
    assert session.get_cart() == [
        {
            "product_id": "prod_1",
            "product_name": "Headphones",
            "price_minor": 2500,
            "category": "Audio",
            "quantity": 2,
            "line_total_minor": 5000,
        }
    ]
    assert session.get_messages()[-1]["content"] == "Added headphones to your cart."
    assert ui.session_state[session.AWAITING_CONFIRMATION] is False
    assert ui.session_state[session.CURRENT_RUN_ID] is None


def test_message_after_cart_confirmation_starts_fresh_intent(monkeypatch):
    ui = _set_session(monkeypatch)
    session.set_user("token", "C000001", "customer", "Ava")
    ui.session_state[session.AWAITING_CONFIRMATION] = False
    classify_intent = Mock(return_value="order_placement")
    monkeypatch.setattr(chat, "classify_intent", classify_intent)
    create_ticket = Mock(return_value={"success": True, "ticket_id": "ticket_2"})
    monkeypatch.setattr(chat.api, "create_ticket", create_ticket)
    monkeypatch.setattr(
        chat.api,
        "start_run",
        Mock(return_value={"success": True, "run_id": "run_2"}),
    )
    monkeypatch.setattr(
        chat.api,
        "poll_run",
        Mock(
            return_value={
                "success": True,
                "status": "paused",
                "final_response": "Which keyboard would you like?",
            }
        ),
    )

    chat._handle_message("Add a keyboard too")

    classify_intent.assert_called_once_with("Add a keyboard too")
    create_ticket.assert_called_once_with("token", "order_placement", "Add a keyboard too")
    assert ui.session_state[session.AWAITING_CONFIRMATION] is True


def test_cart_continuation_reuses_ticket_and_skips_intent_detection(monkeypatch):
    ui = _set_session(monkeypatch)
    session.set_user("token", "C000001", "customer", "Ava")
    session.add_to_cart(
        {
            "product_id": "prod_1",
            "price_minor": 1250,
            "line_total_minor": 1250,
        }
    )
    ui.session_state["current_category"] = "order_placement"
    ui.session_state["ticket_id"] = "ticket_1"
    ui.session_state[session.CURRENT_RUN_ID] = "old_run"
    ui.session_state["run_id"] = "old_run"
    classify_intent = Mock()
    monkeypatch.setattr(chat, "classify_intent", classify_intent)
    create_ticket = Mock()
    monkeypatch.setattr(chat.api, "create_ticket", create_ticket)

    def start_run_side_effect(token, ticket_id, message):
        assert ui.session_state["run_id"] is None
        assert message == "Show me keyboards"
        return {"success": True, "run_id": "run_2"}

    start_run = Mock(side_effect=start_run_side_effect)
    monkeypatch.setattr(chat.api, "start_run", start_run)
    monkeypatch.setattr(
        chat.api,
        "poll_run",
        Mock(
            return_value={
                "success": True,
                "status": "paused",
                "final_response": "Which keyboard would you like?",
            }
        ),
    )

    chat._handle_message("Show me keyboards")

    classify_intent.assert_not_called()
    create_ticket.assert_not_called()
    start_run.assert_called_once_with("token", "ticket_1", "Show me keyboards")
    assert ui.session_state["run_id"] == "run_2"
    assert ui.session_state[session.CURRENT_RUN_ID] == "run_2"
    assert ui.session_state[session.CURRENT_TICKET_ID] == "ticket_1"
    assert ui.session_state["ticket_id"] == "ticket_1"


def test_add_to_cart_reply_continues_paused_order_run(monkeypatch):
    ui = _set_session(monkeypatch)
    session.set_user("token", "C000001", "customer", "Ava")
    ui.session_state[session.AWAITING_CONFIRMATION] = True
    ui.session_state[session.CURRENT_RUN_ID] = "run_1"
    monkeypatch.setattr(
        chat.api,
        "send_message",
        Mock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        chat.api,
        "poll_run",
        Mock(
            return_value={
                "success": True,
                "status": "paused",
                "final_response": "Which option did you mean?",
            }
        ),
    )

    chat._handle_message("I'll take the first option")

    chat.api.send_message.assert_called_once_with(
        "token",
        "run_1",
        "I'll take the first option",
    )
    assert ui.session_state[session.AWAITING_CONFIRMATION] is True
    assert session.get_messages()[-1]["content"] == "Which option did you mean?"


def test_confirm_for_product_resumes_paused_order_run(monkeypatch):
    ui = _set_session(monkeypatch)
    session.set_user("token", "C000001", "customer", "Ava")
    ui.session_state[session.AWAITING_CONFIRMATION] = True
    ui.session_state[session.CURRENT_RUN_ID] = "run_1"
    send_message = Mock(return_value={"success": True})
    monkeypatch.setattr(chat.api, "send_message", send_message)
    monkeypatch.setattr(
        chat.api,
        "poll_run",
        Mock(
            return_value={
                "success": True,
                "status": "paused",
                "final_response": "Anything else?",
            }
        ),
    )
    create_ticket = Mock()
    monkeypatch.setattr(chat.api, "create_ticket", create_ticket)

    chat._handle_message("Confirm for headphones")

    send_message.assert_called_once_with("token", "run_1", "Confirm for headphones")
    create_ticket.assert_not_called()
    assert ui.session_state[session.AWAITING_CONFIRMATION] is True


def test_add_to_cart_completed_adds_item_and_clears_confirmation_and_run(monkeypatch):
    ui = _set_session(monkeypatch)
    session.set_user("token", "C000001", "customer", "Ava")
    ui.session_state[session.AWAITING_CONFIRMATION] = True
    ui.session_state[session.CURRENT_RUN_ID] = "run_1"
    monkeypatch.setattr(chat.api, "send_message", Mock(return_value={"success": True}))
    monkeypatch.setattr(
        chat.api,
        "poll_run",
        Mock(
            return_value={
                "success": True,
                "status": "completed",
                "final_response": "Added headphones to your cart.",
                "cart_item": {
                    "product_id": "prod_1",
                    "product_name": "Headphones",
                    "price_minor": 2500,
                    "category": "Audio",
                    "quantity": 2,
                },
            }
        ),
    )

    chat._handle_add_to_cart("Confirm for headphones")

    assert session.get_messages()[-1]["content"] == "Added headphones to your cart."
    assert session.get_cart() == [
        {
            "product_id": "prod_1",
            "product_name": "Headphones",
            "price_minor": 2500,
            "category": "Audio",
            "quantity": 2,
            "line_total_minor": 5000,
        }
    ]
    assert ui.session_state[session.AWAITING_CONFIRMATION] is False
    assert ui.session_state[session.CURRENT_RUN_ID] is None


def test_add_to_cart_failed_clears_confirmation(monkeypatch):
    ui = _set_session(monkeypatch)
    session.set_user("token", "C000001", "customer", "Ava")
    ui.session_state[session.AWAITING_CONFIRMATION] = True
    ui.session_state[session.CURRENT_RUN_ID] = "run_1"
    monkeypatch.setattr(chat.api, "send_message", Mock(return_value={"success": True}))
    monkeypatch.setattr(
        chat.api,
        "poll_run",
        Mock(return_value={"success": True, "status": "failed"}),
    )

    chat._handle_add_to_cart("Confirm for headphones")

    assert session.get_messages()[-1]["content"] == (
        "Something went wrong. Please try again."
    )
    assert ui.session_state[session.AWAITING_CONFIRMATION] is False


def test_new_intent_clears_paused_run_before_creating_ticket(monkeypatch):
    ui = _set_session(monkeypatch)
    session.set_user("token", "C000001", "customer", "Ava")
    ui.session_state[session.AWAITING_CONFIRMATION] = True
    ui.session_state[session.CURRENT_RUN_ID] = "old_run"
    ui.session_state[session.CURRENT_TICKET_ID] = "old_ticket"

    def create_ticket(token, category, message):
        assert ui.session_state[session.AWAITING_CONFIRMATION] is False
        assert ui.session_state[session.CURRENT_RUN_ID] is None
        assert ui.session_state[session.CURRENT_TICKET_ID] is None
        return {"success": True, "ticket_id": "new_ticket"}

    monkeypatch.setattr(chat, "classify_intent", lambda prompt: "order_tracking")
    monkeypatch.setattr(chat.api, "create_ticket", create_ticket)
    monkeypatch.setattr(
        chat.api,
        "start_run",
        Mock(return_value={"success": True, "run_id": "new_run"}),
    )
    monkeypatch.setattr(
        chat.api,
        "poll_run",
        Mock(
            return_value={
                "success": True,
                "status": "completed",
                "final_response": "Shipment is moving.",
            }
        ),
    )

    chat._handle_message("Where is my package?")

    assert ui.session_state[session.CURRENT_TICKET_ID] == "new_ticket"
    assert ui.session_state[session.CURRENT_RUN_ID] == "new_run"


def test_messages_render_in_history_container_before_chat_input(monkeypatch):
    ui = FakeChatPageStreamlit(["new message", None])
    monkeypatch.setattr(chat, "st", ui)
    monkeypatch.setattr(session, "st", ui)
    monkeypatch.setattr(chat, "_render_sidebar", lambda: None)
    ui.session_state[session.MESSAGES] = [
        {"role": "assistant", "content": "oldest"},
        {"role": "user", "content": "newer"},
    ]
    monkeypatch.setattr(
        chat.cb,
        "render_message",
        lambda role, content: ui.events.append((role, content)),
    )
    handle_message = Mock()
    monkeypatch.setattr(chat, "_handle_message", handle_message)

    chat.render()
    chat.render()

    rendered_messages = [event for event in ui.events if isinstance(event, tuple)]
    assert rendered_messages == [
        ("assistant", "oldest"),
        ("user", "newer"),
        ("assistant", "oldest"),
        ("user", "newer"),
        ("user", "new message"),
    ]
    assert ui.container_kwargs == [{}, {}]
    assert len(ui.markdown_calls) == 2
    for body, options in ui.markdown_calls:
        assert ".block-container" in body
        assert ".stChatInput" in body
        assert options == {"unsafe_allow_html": True}
    assert ui.events.index(("user", "new message")) < len(ui.events) - 1
    handle_message.assert_called_once_with("new message")


def test_paused_refund_stores_pending_approval(monkeypatch):
    ui = _set_session(monkeypatch)
    session.set_user("token", "C000001", "customer", "Ava")
    monkeypatch.setattr(chat, "classify_intent", lambda prompt: "refund")
    monkeypatch.setattr(
        chat.api,
        "create_ticket",
        Mock(return_value={"success": True, "ticket_id": "ticket_2"}),
    )
    monkeypatch.setattr(
        chat.api,
        "start_run",
        Mock(return_value={"success": True, "run_id": "run_2"}),
    )
    monkeypatch.setattr(
        chat.api,
        "poll_run",
        Mock(
            return_value={
                "success": True,
                "status": "paused",
                "final_response": "A reviewer will check your request.",
            }
        ),
    )

    chat._handle_message("Please refund my order")

    assert ui.session_state[session.PENDING_APPROVAL] == {
        "run_id": "run_2",
        "ticket_id": "ticket_2",
        "message": "A reviewer will check your request.",
    }