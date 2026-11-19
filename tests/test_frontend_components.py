from contextlib import nullcontext

from frontend.components import approval_card, chat_bubble, order_card


class FakeStreamlit:
    def __init__(self, clicked_button=None):
        self.clicked_button = clicked_button
        self.chat_roles = []
        self.markdown_calls = []
        self.writes = []
        self.subheaders = []

    def chat_message(self, role):
        self.chat_roles.append(role)
        return nullcontext()

    def markdown(self, content, **kwargs):
        self.markdown_calls.append((content, kwargs))

    def container(self, **kwargs):
        assert kwargs == {"border": True}
        return nullcontext()

    def subheader(self, content):
        self.subheaders.append(content)

    def write(self, content):
        self.writes.append(content)

    def columns(self, count):
        return [nullcontext() for _ in range(count)]

    def button(self, label, **kwargs):
        return label == self.clicked_button


def test_chat_bubble_renders_role_and_content(monkeypatch):
    ui = FakeStreamlit()
    monkeypatch.setattr(chat_bubble, "st", ui)

    chat_bubble.render_message("assistant", "Hello")

    assert ui.chat_roles == ["assistant"]
    assert ui.markdown_calls == [("Hello", {})]


def test_order_card_renders_total_items_and_status_badge(monkeypatch):
    ui = FakeStreamlit()
    monkeypatch.setattr(order_card, "st", ui)

    order_card.render_order_card(
        {
            "order_id": "ord_1",
            "status": "delivered",
            "total_minor": 12550,
            "created_at": "2026-11-19T00:00:00Z",
            "items": [
                {
                    "product_name": "Desk",
                    "quantity": 2,
                    "price_minor": 6275,
                }
            ],
        }
    )

    assert ui.subheaders == ["ord_1"]
    assert any("#15803d" in text for text, _ in ui.markdown_calls)
    assert "Total: ₹125.50" in ui.writes
    assert "• Desk x2 — ₹62.75" in ui.writes


def test_approval_card_shows_evidence_and_returns_clicked_decision(monkeypatch):
    ui = FakeStreamlit(clicked_button="Reject")
    monkeypatch.setattr(approval_card, "st", ui)
    approval = {
        "approval_id": "apr_1",
        "proposal_id": "prp_1",
        "action_type": "refund",
        "payload": {
            "amount_minor": 12500,
            "reason": "Damaged item",
            "evidence_refs": ["order_1"],
            "policy_refs": ["refund-policy:2"],
        },
        "ticket_summary": "The package arrived damaged.",
        "customer_id": "C000001",
        "order_id": "ord_1",
        "expires_at": "2026-11-20T00:00:00Z",
    }

    decision = approval_card.render_approval_card(approval)

    assert decision == "rejected"
    assert ui.subheaders == ["Refund | Customer C000001 | Order ord_1"]
    assert "Amount: ₹125.00" in ui.writes
    assert "Reason: Damaged item" in ui.writes
    assert "Ticket summary: The package arrived damaged." in ui.writes
    assert "Expires: 2026-11-20T00:00:00Z" in ui.writes
