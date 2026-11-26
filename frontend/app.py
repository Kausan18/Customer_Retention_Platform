"""Streamlit entry point and session-based page router."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load .env before any app.* imports resolve
from app.main import load_environment
load_environment(Path(__file__).resolve().parents[1] / ".env")
import streamlit as st

from frontend.pages import chat, login, payment, reviewer, signup
from frontend.utils import session


def main() -> None:
    st.set_page_config(
        page_title="TechMart Support",
        page_icon="🛒",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    session.init_session_state()

    if "page" not in st.session_state:
        st.session_state["page"] = "login"

    page = st.session_state.get("page", "login")

    if page == "signup":
        signup.render()
    elif page == "reviewer":
        reviewer.render()
    elif page == "chat":
        if not session.is_logged_in():
            st.session_state["page"] = "login"
            st.rerun()
            return
        if session.get_role() == "reviewer":
            st.session_state["page"] = "reviewer"
            st.rerun()
            return
        chat.render()
    elif page == "payment":
        if not session.is_logged_in():
            st.session_state["page"] = "login"
            st.rerun()
            return
        payment.render()
    else:
        login.render()


if __name__ == "__main__":
    main()
