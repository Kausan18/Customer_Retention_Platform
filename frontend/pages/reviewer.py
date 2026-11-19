"""Reviewer login and proposal approval queue."""

import streamlit as st

from frontend.components import approval_card
from frontend.utils import api, session


def render() -> None:
    if session.is_logged_in() and session.get_role() == "reviewer":
        _render_approval_queue()
    else:
        _render_reviewer_login()


def _render_reviewer_login() -> None:
    _, content, _ = st.columns([1, 2, 1])
    with content:
        st.subheader("Reviewer Login")
        reviewer_id = st.text_input("Reviewer ID", key="reviewer_id")
        pin = st.text_input("PIN", type="password", key="reviewer_pin")

        if st.button("Login", key="reviewer_login", type="primary"):
            result = api.reviewer_login(reviewer_id, pin)
            if result["success"]:
                session.set_user(
                    result["token"],
                    reviewer_id,
                    role="reviewer",
                    name=result["name"],
                )
                st.rerun()
            else:
                st.error(result["error"])

        st.divider()
        with st.expander("Demo Reviewer Credentials", expanded=True):
            st.markdown("""
    | Reviewer ID | PIN  |
    |-------------|------|
    | R000001     | 9999 |
    | R000002     | 9999 |
    """)
            st.caption("These accounts are pre-seeded. No signup required for reviewers.")


def _render_approval_queue() -> None:
    token = session.get_token()
    if token is None:
        st.error("Reviewer session is missing an authentication token.")
        return

    title_column, name_column, logout_column = st.columns([3, 1, 1])
    with title_column:
        st.header("Pending Approvals")
    with name_column:
        st.write(session.get_name())
    with logout_column:
        if st.button("Logout", key="reviewer_logout"):
            api.logout(token)
            session.clear_user()
            st.rerun()

    result = api.get_approvals(token)
    if not result["success"]:
        st.error(result["error"])
        return

    approvals = result["approvals"]
    if not approvals:
        st.info("No pending approvals at this time.")
        return

    for approval in approvals:
        decision = approval_card.render_approval_card(approval)
        if decision is None:
            continue

        result = api.submit_decision(token, approval["approval_id"], decision)
        if result["success"]:
            st.success(f"Proposal {decision}.")
            st.rerun()
        else:
            st.error(result["error"])
