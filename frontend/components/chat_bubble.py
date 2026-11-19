"""Render a user or assistant message as a styled chat bubble."""

import streamlit as st


def render_message(role: str, content: str) -> None:
    with st.chat_message(role):
        st.markdown(content)
