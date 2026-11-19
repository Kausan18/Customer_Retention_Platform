"""Customer chat page with workflow handling and a cart sidebar."""

import re
import streamlit as st

from frontend.components import chat_bubble as cb, order_card as oc
from frontend.utils import api, detect_category as detect, session


_CART_ADDITION_KEYWORDS = (
    "add",
    "also add",
    "add item",
    "i want",
    "and also",
    "confirm for",
)
_CHECKOUT_KEYWORDS = (
    "checkout",
    "done adding",
    "that's all",
    "place order",
    "order now",
    "confirm cart",
    "pay",
)
def _normalize_text(text: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", text.lower()).split())


def _matches_any(text: str, keywords: tuple[str, ...]) -> bool:
    normalized_text = _normalize_text(text)
    padded_text = f" {normalized_text} "
    return any(
        f" {_normalize_text(keyword)} " in padded_text
        for keyword in keywords
    )


def render() -> None:
    title_column, logout_column = st.columns([4, 1])
    with title_column:
        st.header("TechMart Customer Support")
    with logout_column:
        if st.button("Logout", key="chat_logout", type="tertiary"):
            api.logout(session.get_token())
            session.clear_user()
            st.session_state["page"] = "login"
            st.rerun()
            return

    if session.is_payment_ready():
        st.info("Your cart is ready. Proceeding to payment...")
        st.session_state["page"] = "payment"
        st.rerun()
        return

    with st.sidebar:
        _render_sidebar()

    messages_container = st.container(height=600, border=False)
    with messages_container:
        for message in session.get_messages():
            cb.render_message(message["role"], message["content"])

    prompt = st.chat_input("Type your message...")
    if prompt:
        session.add_message("user", prompt)
        _handle_message(prompt)
        st.rerun()


def _handle_message(prompt: str) -> None:
    if _matches_any(prompt, _CHECKOUT_KEYWORDS) and session.get_cart():
        session.set_payment_ready(True)
        session.add_message(
            "assistant",
            f"Taking you to payment. Your cart total is "
            f"₹{session.get_cart_total() / 100:.2f}.",
        )
        return

    category = detect.detect_category(prompt)
    if st.session_state.get(session.AWAITING_CONFIRMATION, False):
        if (
            _matches_any(prompt, _CART_ADDITION_KEYWORDS)
            or category == "order_placement"
        ):
            _handle_add_to_cart(prompt)
            return

        st.session_state[session.AWAITING_CONFIRMATION] = False
        st.session_state[session.CURRENT_RUN_ID] = None
        st.session_state[session.CURRENT_TICKET_ID] = None

    token = session.get_token()
    ticket_result = api.create_ticket(token, category, prompt)
    if not ticket_result.get("success"):
        session.add_message(
            "assistant",
            f"Sorry, I couldn't process that: {ticket_result.get('error', 'Unknown error')}",
        )
        return

    ticket_id = ticket_result["ticket_id"]
    run_result = api.start_run(token, ticket_id)
    if not run_result.get("success"):
        session.add_message(
            "assistant",
            "Something went wrong starting the workflow.",
        )
        return

    run_id = run_result["run_id"]
    st.session_state[session.CURRENT_RUN_ID] = run_id
    st.session_state[session.CURRENT_TICKET_ID] = ticket_id

    result = api.poll_run(token, run_id)
    if result.get("success") is False:
        session.add_message(
            "assistant",
            f"I couldn't check the workflow status: {result.get('error', 'Unknown error')}",
        )
        return

    status = result.get("status")
    if status == "completed":
        session.add_message(
            "assistant",
            result.get("final_response") or "Your request is complete.",
        )
    elif status == "paused" and category == "order_placement":
        _handle_placement_pause(result)
    elif status == "paused" and category in ("refund", "exchange"):
        _handle_approval_pause(result)
    elif status == "failed":
        session.add_message(
            "assistant",
            "I encountered an issue. Please try again or contact support.",
        )
    else:
        session.add_message(
            "assistant",
            "I'm still working on your request. Please check back shortly.",
        )


def _handle_placement_pause(result: dict) -> None:
    session.add_message(
        "assistant",
        result.get("final_response") or "Please choose one of the listed products.",
    )
    st.session_state[session.AWAITING_CONFIRMATION] = True


def _handle_add_to_cart(prompt: str) -> None:
    token = session.get_token()
    run_id = st.session_state.get(session.CURRENT_RUN_ID)
    if not run_id:
        session.add_message(
            "assistant",
            "I couldn't continue the previous order request. Please start again.",
        )
        return

    send_result = api.send_message(token, run_id, prompt)
    if not send_result.get("success"):
        session.add_message(
            "assistant",
            f"I couldn't continue the order request: {send_result.get('error', 'Unknown error')}",
        )
        return

    result = api.poll_run(token, run_id)
    if result.get("success") is False:
        session.add_message(
            "assistant",
            f"I couldn't check the order status: {result.get('error', 'Unknown error')}",
        )
        return

    status = result.get("status")
    if status == "completed":
        session.add_message(
            "assistant",
            result["final_response"],
        )
        st.session_state[session.AWAITING_CONFIRMATION] = False
        st.session_state[session.CURRENT_RUN_ID] = None
    elif status == "paused":
        session.add_message(
            "assistant",
            result["final_response"],
        )
        st.session_state[session.AWAITING_CONFIRMATION] = True
    elif status == "failed":
        session.add_message(
            "assistant",
            "Something went wrong. Please try again.",
        )
        st.session_state[session.AWAITING_CONFIRMATION] = False
    else:
        session.add_message(
            "assistant",
            "I encountered an issue. Please try again or contact support.",
        )


def _handle_approval_pause(result: dict) -> None:
    session.add_message(
        "assistant",
        "Your request is being reviewed. You'll see the status update here once a decision is made.",
    )
    session.set_pending_approval(
        {
            "run_id": st.session_state[session.CURRENT_RUN_ID],
            "ticket_id": st.session_state[session.CURRENT_TICKET_ID],
            "message": result.get("final_response"),
        }
    )


def _render_sidebar() -> None:
    if st.button("➕ New Chat", use_container_width=True):
        session.init_new_chat_session()
        st.rerun()
        return

    st.subheader("🛒 Cart")
    cart = session.get_cart()
    if not cart:
        st.caption("Your cart is empty")
    else:
        for index, item in enumerate(cart):
            st.write(
                f"{item['product_name']} x{item['quantity']} — "
                f"₹{item['line_total_minor'] / 100:.2f}"
            )
            if st.button(
                "Remove",
                key=f"remove_{item['product_id']}_{index}",
                type="tertiary",
            ):
                session.remove_from_cart(item["product_id"])
                st.rerun()
                return

    st.write(f"Total: ₹{session.get_cart_total() / 100:.2f}")
    if cart and st.button("Proceed to Checkout", key="proceed_to_checkout"):
        session.set_payment_ready(True)
        st.rerun()
        return

    st.divider()
    st.subheader("🕘 Chat History")
    history = session.get_chat_history()
    if not history:
        st.caption("No previous chats yet.")
    else:
        for i, past_session in enumerate(reversed(history)):
            idx = len(history) - 1 - i
            label = f"{past_session['preview']} ({past_session['started_at'][:10]})"
            if st.button(label, key=f"history_{idx}", use_container_width=True):
                session.load_session_from_history(idx)
                st.rerun()
                return

    pending_approval = st.session_state.get(session.PENDING_APPROVAL)
    if pending_approval:
        st.warning("⏳ Awaiting reviewer decision")
        if st.button("Check Status", key="check_approval_status"):
            token = session.get_token()
            result = api.poll_run(token, pending_approval["run_id"])
            if result.get("success") is False:
                st.error(result.get("error", "Could not check the request status."))
            elif result.get("status") == "completed":
                session.add_message(
                    "assistant",
                    result.get("final_response") or "Your request has been reviewed.",
                )
                session.clear_pending_approval()
                st.rerun()
                return
            elif result.get("status") == "failed":
                session.add_message(
                    "assistant",
                    "I encountered an issue processing the reviewed request.",
                )
                session.clear_pending_approval()
                st.rerun()
                return
            else:
                st.info("Your request is still being reviewed.")

    timeline = st.session_state.get(session.WORKFLOW_TIMELINE)
    if timeline:
        st.subheader("Workflow Timeline")
        for event in timeline:
            st.caption(
                f"{event.get('event_type', 'Event')} - "
                f"{event.get('created_at', '')}"
            )
