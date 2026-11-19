"""Show the cart summary and collect mock payment confirmation."""

from datetime import date, timedelta
import re
import streamlit as st

from frontend.utils import api, session


_PAYMENT_METHODS = [
    "UPI",
    "Credit Card",
    "Debit Card",
    "Net Banking",
    "Cash on Delivery",
]
_BANKS = ["SBI", "HDFC", "ICICI", "Axis", "Kotak"]


def _validate_payment_details(
    payment_method: str,
    upi_id: str,
    card_number: str,
    expiry: str,
    cvv: str,
) -> str | None:
    if payment_method == "UPI":
        if "@" not in upi_id:
            return "Enter a valid UPI ID containing '@'."
    elif payment_method in ("Credit Card", "Debit Card"):
        normalized_card_number = re.sub(r"[\s-]", "", card_number)
        if not re.fullmatch(r"\d{16}", normalized_card_number):
            return "Enter a valid 16-digit card number."
        if not re.fullmatch(r"(0[1-9]|1[0-2])/\d{2}", expiry.strip()):
            return "Enter the card expiry in MM/YY format."
        if not re.fullmatch(r"\d{3}", cvv.strip()):
            return "Enter a valid 3-digit CVV."
    return None


def render() -> None:
    st.markdown(
        "<style>h1, h2, h3 {text-align: center;}</style>",
        unsafe_allow_html=True,
    )
    _, content, _ = st.columns([1, 2, 1])
    with content:
        st.title("Complete Your Payment")
        st.subheader("Order Summary")

        cart = session.get_cart()
        rows = [
            {
                "Product": item["product_name"],
                "Quantity": item["quantity"],
                "Unit Price": f"₹{item['price_minor'] / 100:.2f}",
                "Line Total": f"₹{item['line_total_minor'] / 100:.2f}",
            }
            for item in cart
        ]
        if rows:
            st.table(rows)
        else:
            st.info("Your cart is empty.")

        total_minor = session.get_cart_total()
        st.markdown(f"## **Grand Total: ₹{total_minor / 100:.2f}**")
        order_date = date.today()
        st.write(f"Order Date: {order_date.strftime('%d %b %Y')}")
        st.write(
            "Estimated Delivery: "
            f"{(order_date + timedelta(days=5)).strftime('%d %b %Y')}"
        )

        payment_method = st.radio(
            "Select Payment Method",
            _PAYMENT_METHODS,
            key="payment_method",
        )

        upi_id = ""
        card_number = ""
        expiry = ""
        cvv = ""
        if payment_method == "UPI":
            upi_id = st.text_input(
                "UPI ID",
                placeholder="name@upi",
                key="payment_upi_id",
            )
        elif payment_method in ("Credit Card", "Debit Card"):
            card_number = st.text_input(
                "Card Number",
                type="password",
                max_chars=19,
                key="payment_card_number",
            )
            expiry = st.text_input(
                "Expiry (MM/YY)",
                placeholder="MM/YY",
                max_chars=5,
                key="payment_card_expiry",
            )
            cvv = st.text_input(
                "CVV",
                type="password",
                max_chars=3,
                key="payment_card_cvv",
            )
        elif payment_method == "Net Banking":
            st.selectbox("Select Bank", _BANKS, key="payment_bank")
        elif payment_method == "Cash on Delivery":
            st.info(f"You will pay ₹{total_minor / 100:.2f} at the time of delivery.")

        pay_column, back_column = st.columns([1, 1])
        with pay_column:
            pay_clicked = st.button("Pay Now", type="primary", key="pay_now")
        with back_column:
            back_clicked = st.button(
                "Back to Cart",
                type="secondary",
                key="back_to_cart",
            )

        if pay_clicked:
            if not cart:
                st.error("Your cart is empty.")
                return

            validation_error = _validate_payment_details(
                payment_method,
                upi_id,
                card_number,
                expiry,
                cvv,
            )
            if validation_error:
                st.error(validation_error)
                return

            token = session.get_token()
            if token is None:
                st.error("Your session has expired. Please log in again.")
                return

            result = api.place_order(token, cart, payment_method)
            if result.get("success"):
                session.add_message(
                    "assistant",
                    f"Your order {result['order_id']} has been placed successfully! "
                    f"Total paid: ₹{result['total_minor'] / 100:.2f}. "
                    "Estimated delivery in 5 days.",
                )
                session.clear_cart()
                session.set_payment_ready(False)
                st.session_state["page"] = "chat"
                st.rerun()
                return

            st.error(result.get("error", "Payment failed. Please try again."))
        elif back_clicked:
            session.set_payment_ready(False)
            st.session_state["page"] = "chat"
            st.rerun()
