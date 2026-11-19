"""Render a reviewer proposal card with its evidence fields."""

import streamlit as st


def render_approval_card(approval: dict) -> str | None:
    payload = approval["payload"]
    ticket = approval.get("ticket", {})
    customer_id = approval.get("customer_id", ticket.get("customer_id", "-"))
    order_id = approval.get("order_id", ticket.get("order_id")) or "-"
    action_type = approval["action_type"]
    ticket_summary = approval.get(
        "ticket_summary",
        ticket.get("summary", "-"),
    )
    approval_id = approval["approval_id"]
    amount_minor = payload.get(
        "amount_minor",
        payload.get("replacement_price_minor"),
    )

    with st.container(border=True):
        st.subheader(
            f"{action_type.title()} | Customer {customer_id} | Order {order_id}"
        )
        st.write(
            f"Amount: {'-' if amount_minor is None else f'₹{amount_minor / 100:.2f}'}"
        )
        st.write(f"Reason: {payload.get('reason', '-')}")
        st.write(f"Evidence refs: {payload.get('evidence_refs', [])}")
        st.write(f"Policy refs: {payload.get('policy_refs', [])}")
        st.write(f"Ticket summary: {ticket_summary}")
        st.write(f"Expires: {approval['expires_at']}")

        approve_column, reject_column = st.columns(2)
        with approve_column:
            if st.button(
                "Approve",
                key=f"approve_{approval_id}",
                type="primary",
            ):
                return "approved"
        with reject_column:
            if st.button(
                "Reject",
                key=f"reject_{approval_id}",
                type="secondary",
            ):
                return "rejected"

    return None
