"""Customer login page with customer ID, PIN, and account navigation."""

import streamlit as st

from frontend.utils import api, session


def render() -> None:
    st.markdown(
        "<style>h1, h2, h3 {text-align: center;}</style>",
        unsafe_allow_html=True,
    )
    _, content, _ = st.columns([1, 2, 1])

    with content:
        st.title("TechMart Support")
        signup_success = st.session_state.get("signup_success")
        if signup_success is not None:
            st.success(signup_success)
        st.subheader("Customer Login")

        customer_id = st.text_input("Customer ID", key="login_customer_id")
        pin = st.text_input("PIN", type="password", key="login_pin")

        if st.button("Login", key="customer_login", type="primary"):
            if not customer_id.strip() or not pin.strip():
                st.error("Please enter both your customer ID and PIN.")
            else:
                result = api.login(customer_id.strip(), pin)
                if result["success"]:
                    session.set_user(
                        result["token"],
                        result["customer_id"],
                        role="customer",
                        name=result["name"],
                    )
                    st.session_state["signup_success"] = None
                    st.session_state["page"] = "chat"
                    st.rerun()
                elif result.get("error") == "reviewer_account":
                    st.warning(
                        "This is a reviewer account. Please use the reviewer login."
                    )
                else:
                    st.error(result["error"])

        if st.button(
            "New here? Create an account",
            key="go_to_signup",
            type="tertiary",
        ):
            st.session_state["signup_success"] = None
            st.session_state["page"] = "signup"
            st.rerun()

        st.divider()

        if st.button(
            "Reviewer? Login here",
            key="go_to_reviewer",
            type="tertiary",
        ):
            st.session_state["signup_success"] = None
            st.session_state["page"] = "reviewer"
            st.rerun()
