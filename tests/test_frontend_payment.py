from contextlib import nullcontext
from unittest.mock import Mock

from frontend.pages import payment
from frontend.utils import session


class FakeStreamlit:
    def __init__(self, values=None, selected_method="UPI", clicked_button=None):
        self.session_state = {}
        self.values = values or {}
        self.selected_method = selected_method
        self.clicked_button = clicked_button
        self.errors = []
        self.infos = []
        self.tables = []
        self.reruns = 0

    def markdown(self, *args, **kwargs):
        pass

    def columns(self, spec):
        return [nullcontext() for _ in spec]

    def title(self, *args, **kwargs):
        pass

    def subheader(self, *args, **kwargs):
        pass

    def table(self, rows):
        self.tables.append(rows)

    def info(self, message):
        self.infos.append(message)

    def write(self, *args, **kwargs):
        pass

    def radio(self, *args, **kwargs):
        return self.selected_method

    def text_input(self, label, **kwargs):
        return self.values.get(label, "")

    def selectbox(self, *args, **kwargs):
        return "SBI"

    def button(self, label, **kwargs):
        return label == self.clicked_button

    def error(self, message):
        self.errors.append(message)

    def rerun(self):
        self.reruns += 1


def _setup(monkeypatch, ui):
    monkeypatch.setattr(payment, "st", ui)
    monkeypatch.setattr(session, "st", ui)
    session.set_user("token", "C000001", "customer", "Ava")
    ui.session_state[session.CART] = [
        {
            "product_id": "prod_1",
            "product_name": "Headphones",
            "quantity": 2,
            "price_minor": 1250,
            "line_total_minor": 2500,
        }
    ]
    ui.session_state[session.CART_TOTAL] = 2500
    session.set_payment_ready(True)


def test_successful_payment_places_order_and_returns_to_chat(monkeypatch):
    ui = FakeStreamlit(
        values={"UPI ID": "ava@upi"},
        clicked_button="Pay Now",
    )
    _setup(monkeypatch, ui)
    expected_cart = list(session.get_cart())
    place_order = Mock(
        return_value={
            "success": True,
            "order_id": "ord_123",
            "total_minor": 2500,
        }
    )
    monkeypatch.setattr(payment.api, "place_order", place_order)

    payment.render()

    place_order.assert_called_once_with(
        "token",
        expected_cart,
        "UPI",
    )
    assert ui.session_state["page"] == "chat"
    assert session.get_cart() == []
    assert session.get_cart_total() == 0
    assert not session.is_payment_ready()
    assert "ord_123" in session.get_messages()[-1]["content"]
    assert ui.reruns == 1


def test_invalid_card_details_do_not_place_order(monkeypatch):
    ui = FakeStreamlit(
        values={
            "Card Number": "123456789012345",
            "Expiry (MM/YY)": "12/29",
            "CVV": "123",
        },
        selected_method="Credit Card",
        clicked_button="Pay Now",
    )
    _setup(monkeypatch, ui)
    place_order = Mock()
    monkeypatch.setattr(payment.api, "place_order", place_order)

    payment.render()

    place_order.assert_not_called()
    assert ui.errors == ["Enter a valid 16-digit card number."]
    assert ui.reruns == 0


def test_back_to_cart_returns_without_placing_order(monkeypatch):
    ui = FakeStreamlit(clicked_button="Back to Cart")
    _setup(monkeypatch, ui)
    place_order = Mock()
    monkeypatch.setattr(payment.api, "place_order", place_order)

    payment.render()

    place_order.assert_not_called()
    assert ui.session_state["page"] == "chat"
    assert not session.is_payment_ready()
    assert len(session.get_cart()) == 1
    assert ui.reruns == 1
