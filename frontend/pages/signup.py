"""New customer registration page for account and delivery address details."""

import re

import streamlit as st

from frontend.utils import api


def render() -> None:
    st.markdown(
        "<style>h1, h2, h3 {text-align: center;}</style>",
        unsafe_allow_html=True,
    )
    _, content, _ = st.columns([1, 2, 1])

    with content:
        st.title("TechMart Support")
        st.subheader("Create Account")

        name = st.text_input("Full Name", key="signup_name")
        email = st.text_input("Email", key="signup_email")
        phone = st.text_input("Phone (optional)", key="signup_phone")
        pin = st.text_input("PIN", type="password", key="signup_pin")
        confirm_pin = st.text_input(
            "Confirm PIN",
            type="password",
            key="signup_confirm_pin",
        )

        st.subheader("Delivery Address")
        address_line1 = st.text_input("Address Line 1", key="signup_address_line1")
        address_line2 = st.text_input(
            "Address Line 2 (optional)",
            key="signup_address_line2",
        )
        city = st.text_input("City", key="signup_city")
        state = st.text_input("State", key="signup_state")
        pincode = st.text_input("Pincode", key="signup_pincode")

        if st.button("Create Account", key="create_account", type="primary"):
            errors = []
            if not name.strip():
                errors.append("Full Name is required.")
            if "@" not in email or "." not in email:
                errors.append("Enter a valid email address.")
            if not re.fullmatch(r"[0-9]{4}", pin):
                errors.append("PIN must be exactly 4 digits.")
            if pin != confirm_pin:
                errors.append("PIN and Confirm PIN must match.")
            if not address_line1.strip():
                errors.append("Address Line 1 is required.")
            if not city.strip():
                errors.append("City is required.")
            if not state.strip():
                errors.append("State is required.")
            if not pincode.strip():
                errors.append("Pincode is required.")
            if not re.fullmatch(r"[0-9]{6}", pincode):
                errors.append("Pincode must be exactly 6 digits.")

            if errors:
                for error in errors:
                    st.error(error)
            else:
                result = api.signup(
                    name.strip(),
                    email.strip(),
                    phone.strip(),
                    pin,
                    address_line1.strip(),
                    address_line2.strip(),
                    city.strip(),
                    state.strip(),
                    pincode.strip(),
                )
                if result["success"]:
                    st.session_state["signup_success"] = result["message"]
                    st.session_state["page"] = "login"
                    st.rerun()
                else:
                    st.error(result["error"])

        if st.button(
            "Already have an account? Login",
            key="go_to_login",
            type="tertiary",
        ):
            st.session_state["page"] = "login"
            st.rerun()
