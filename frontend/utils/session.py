"""Define Streamlit session-state keys and user and cart helpers."""

from datetime import datetime
from typing import Literal

import streamlit as st


TOKEN = "token"
CUSTOMER_ID = "customer_id"
ROLE = "role"
NAME = "name"
MESSAGES = "messages"
CURRENT_RUN_ID = "current_run_id"
CURRENT_TICKET_ID = "current_ticket_id"
PENDING_APPROVAL = "pending_approval"
CART = "cart"
CART_TOTAL = "cart_total"
AWAITING_CONFIRMATION = "awaiting_confirmation"
PAYMENT_READY = "payment_ready"
WORKFLOW_TIMELINE = "workflow_timeline"
CHAT_HISTORY = "chat_history"
SESSION_START_TIME = "session_start_time"


def is_logged_in() -> bool:
    return TOKEN in st.session_state and st.session_state[TOKEN] is not None


def get_token() -> str | None:
    return st.session_state.get(TOKEN)


def get_role() -> str | None:
    return st.session_state.get(ROLE)


def get_customer_id() -> str | None:
    return st.session_state.get(CUSTOMER_ID)


def get_name() -> str | None:
    return st.session_state.get(NAME)


def set_user(token: str, customer_id: str, role: str, name: str) -> None:
    st.session_state[TOKEN] = token
    st.session_state[CUSTOMER_ID] = customer_id
    st.session_state[ROLE] = role
    st.session_state[NAME] = name
    st.session_state[SESSION_START_TIME] = datetime.utcnow().isoformat()


def save_current_session_to_history() -> None:
    messages = st.session_state.get(MESSAGES, [])
    if not messages:
        return

    started_at = st.session_state.get(SESSION_START_TIME, "Unknown time")
    first_user_message = next(
        (
            str(message.get("content", ""))
            for message in messages
            if message.get("role") == "user"
        ),
        "",
    )
    preview = first_user_message[:40]
    if len(first_user_message) > 40:
        preview += "..."

    history = st.session_state.get(CHAT_HISTORY)
    if history is None:
        history = []
        st.session_state[CHAT_HISTORY] = history
    history.append(
        {
            "started_at": started_at,
            "preview": preview,
            "messages": [dict(message) for message in messages],
        }
    )
    if len(history) > 10:
        del history[:-10]


def init_new_chat_session() -> None:
    save_current_session_to_history()
    st.session_state[MESSAGES] = []
    st.session_state[CURRENT_RUN_ID] = None
    st.session_state[CURRENT_TICKET_ID] = None
    st.session_state[PENDING_APPROVAL] = None
    st.session_state[AWAITING_CONFIRMATION] = False
    st.session_state[PAYMENT_READY] = False
    st.session_state[WORKFLOW_TIMELINE] = []
    st.session_state[SESSION_START_TIME] = datetime.utcnow().isoformat()


def get_chat_history() -> list:
    return st.session_state.get(CHAT_HISTORY, [])


def load_session_from_history(index: int) -> None:
    past_session = get_chat_history()[index]
    st.session_state[MESSAGES] = [
        dict(message) for message in past_session["messages"]
    ]
    st.session_state[CURRENT_RUN_ID] = None
    st.session_state[CURRENT_TICKET_ID] = None
    st.session_state[AWAITING_CONFIRMATION] = False


def clear_user() -> None:
    st.session_state.clear()


def add_message(role: Literal["user", "assistant"], content: str) -> None:
    messages = st.session_state.get(MESSAGES)
    if messages is None:
        messages = []
        st.session_state[MESSAGES] = messages
    messages.append({"role": role, "content": content})


def get_messages() -> list[dict[str, str]]:
    messages = st.session_state.get(MESSAGES)
    if messages is None:
        messages = []
        st.session_state[MESSAGES] = messages
    return messages


def _recalculate_cart_total() -> None:
    st.session_state[CART_TOTAL] = sum(
        item["line_total_minor"] for item in st.session_state[CART]
    )


def add_to_cart(product: dict) -> None:
    cart = st.session_state.get(CART)
    if cart is None:
        cart = []
        st.session_state[CART] = cart
    cart.append(product)
    _recalculate_cart_total()


def remove_from_cart(product_id: str) -> None:
    cart = st.session_state.get(CART)
    if cart is None:
        cart = []
        st.session_state[CART] = cart
    st.session_state[CART] = [
        item for item in cart if item["product_id"] != product_id
    ]
    _recalculate_cart_total()


def get_cart() -> list[dict]:
    cart = st.session_state.get(CART)
    if cart is None:
        cart = []
        st.session_state[CART] = cart
    return cart


def clear_cart() -> None:
    st.session_state[CART] = []
    st.session_state[CART_TOTAL] = 0


def get_cart_total() -> int:
    return st.session_state.get(CART_TOTAL, 0)


def set_pending_approval(approval_dict: dict) -> None:
    st.session_state[PENDING_APPROVAL] = approval_dict


def clear_pending_approval() -> None:
    st.session_state[PENDING_APPROVAL] = None


def set_payment_ready(val: bool) -> None:
    st.session_state[PAYMENT_READY] = val


def is_payment_ready() -> bool:
    return bool(st.session_state.get(PAYMENT_READY, False))
