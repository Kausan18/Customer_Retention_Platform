from contextlib import nullcontext
from unittest.mock import Mock

from frontend.components import approval_card
from frontend.pages import reviewer
from frontend.utils import session


class FakeStreamlit:
    def __init__(self, clicked_button=None):
        self.session_state = {}
        self.clicked_button = clicked_button
        self.errors = []
        self.infos = []
        self.successes = []
        self.reruns = 0

    def columns(self, spec):
        count = spec if isinstance(spec, int) else len(spec)
        return [nullcontext() for _ in range(count)]

    def subheader(self, *args, **kwargs):
        pass

    def header(self, *args, **kwargs):
        pass

    def text_input(self, *args, **kwargs):
        return "R000001" if args[0] == "Reviewer ID" else "1234"

    def button(self, label, **kwargs):
        return label == self.clicked_button

    def write(self, *args, **kwargs):
        pass

    def caption(self, *args, **kwargs):
        pass

    def divider(self):
        pass

    def expander(self, *args, **kwargs):
        return nullcontext()

    def markdown(self, *args, **kwargs):
        pass

    def json(self, *args, **kwargs):
        pass

    def container(self, **kwargs):
        return nullcontext()

    def error(self, message):
        self.errors.append(message)

    def info(self, message):
        self.infos.append(message)

    def success(self, message):
        self.successes.append(message)

    def rerun(self):
        self.reruns += 1


def test_reviewer_login_sets_reviewer_session(monkeypatch):
    ui = FakeStreamlit(clicked_button="Login")
    monkeypatch.setattr(reviewer, "st", ui)
    monkeypatch.setattr(session, "st", ui)
    reviewer_api = Mock(
        return_value={
            "success": True,
            "token": "token",
            "reviewer_id": "R000001",
            "name": "Reviewer One",
        }
    )
    monkeypatch.setattr(reviewer.api, "reviewer_login", reviewer_api)

    reviewer.render()

    reviewer_api.assert_called_once_with("R000001", "1234")
    assert ui.session_state["role"] == "reviewer"
    assert ui.session_state["customer_id"] == "R000001"
    assert ui.reruns == 1


def test_approval_queue_submits_clicked_decision(monkeypatch):
    ui = FakeStreamlit(clicked_button="Approve")
    monkeypatch.setattr(reviewer, "st", ui)
    monkeypatch.setattr(approval_card, "st", ui)
    monkeypatch.setattr(session, "st", ui)
    session.set_user("token", "R000001", "reviewer", "Reviewer One")
    approval = {
        "approval_id": "approval_1",
        "proposal_id": "proposal_1",
        "action_type": "refund",
        "payload": {"amount_minor": 500},
        "expires_at": "2026-11-20T00:00:00Z",
        "ticket": {"customer_id": "C000001", "category": "refund"},
    }
    get_approvals = Mock(return_value={"success": True, "approvals": [approval]})
    submit_decision = Mock(return_value={"success": True})
    monkeypatch.setattr(reviewer.api, "get_approvals", get_approvals)
    monkeypatch.setattr(reviewer.api, "submit_decision", submit_decision)

    reviewer.render()

    get_approvals.assert_called_once_with("token")
    submit_decision.assert_called_once_with("token", "approval_1", "approved")
    assert ui.successes == ["Proposal approved."]
    assert ui.reruns == 1


def test_empty_queue_displays_info(monkeypatch):
    ui = FakeStreamlit()
    monkeypatch.setattr(reviewer, "st", ui)
    monkeypatch.setattr(session, "st", ui)
    session.set_user("token", "R000001", "reviewer", "Reviewer One")
    monkeypatch.setattr(
        reviewer.api,
        "get_approvals",
        Mock(return_value={"success": True, "approvals": []}),
    )

    reviewer.render()

    assert ui.infos == ["No pending approvals at this time."]
