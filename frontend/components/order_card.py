"""Render a compact order summary card with a status badge."""

from html import escape

import streamlit as st


_STATUS_COLORS = {
    "delivered": "#15803d",
    "confirmed": "#15803d",
    "shipped": "#c2410c",
    "processing": "#c2410c",
    "cancelled": "#b91c1c",
}


def render_order_card(order: dict) -> None:
    status = str(order["status"])
    status_key = status.lower()
    badge_color = _STATUS_COLORS.get(status_key, "#64748b")

    with st.container(border=True):
        st.subheader(order["order_id"])
        st.markdown(
            "<span style=\"display:inline-block;padding:0.2rem 0.6rem;"
            f"border-radius:1rem;background-color:{badge_color};color:white;\">"
            f"{escape(status.title())}</span>",
            unsafe_allow_html=True,
        )
        st.write(f"Total: ₹{order['total_minor'] / 100:.2f}")

        for item in order["items"]:
            st.write(
                f"• {item['product_name']} x{item['quantity']} — "
                f"₹{item['price_minor'] / 100:.2f}"
            )
