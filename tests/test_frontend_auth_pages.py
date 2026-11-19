from contextlib import nullcontext
from unittest.mock import Mock

from frontend.pages import login, signup
from frontend.utils import session


class FakeStreamlit:
    def __init__(self, values=None, clicked_button=None):
        self.session_state = {}
        self.values = values or {}
        self.clicked_button = clicked_button
        self.errors = []
        self.warnings = []
        self.successes = []
        self.reruns = 0
        self.events = []

    def markdown(self, *args, **kwargs):
        pass

    def columns(self, spec):
        return [nullcontext() for _ in spec]

    def title(self, *args, **kwargs):
        self.events.append(("title", args[0] if args else ""))

    def subheader(self, *args, **kwargs):
        self.events.append(("subheader", args[0] if args else ""))

    def text_input(self, label, **kwargs):
        self.events.append(("text_input", label))
        return self.values.get(label, "")

    def button(self, label, **kwargs):
        return label == self.clicked_button

    def error(self, message):
        self.errors.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def success(self, message):
        self.successes.append(message)
        self.events.append(("success", message))

    def divider(self):
        pass

    def rerun(self):
        self.reruns += 1


def test_login_sets_customer_session_and_navigates(monkeypatch):
    ui = FakeStreamlit(
        {"Customer ID": "C000001", "PIN": "1234"},
        clicked_button="Login",
    )
    ui.session_state["signup_success"] = "Account created. Customer ID: C000051."
    monkeypatch.setattr(login, "st", ui)
    monkeypatch.setattr(session, "st", ui)
    login_api = Mock(
        return_value={
            "success": True,
            "token": "token",
            "customer_id": "C000001",
            "name": "Ava",
        }
    )
    monkeypatch.setattr(login.api, "login", login_api)

    login.render()

    login_api.assert_called_once_with("C000001", "1234")
    assert ui.session_state["page"] == "chat"
    assert ui.session_state["token"] == "token"
    assert ui.session_state["signup_success"] is None
    assert ui.successes == ["Account created. Customer ID: C000051."]
    assert ui.events.index(("success", ui.successes[0])) < ui.events.index(
        ("text_input", "Customer ID")
    )
    assert ui.reruns == 1


def test_login_shows_reviewer_warning(monkeypatch):
    ui = FakeStreamlit(
        {"Customer ID": "R000001", "PIN": "1234"},
        clicked_button="Login",
    )
    monkeypatch.setattr(login, "st", ui)
    monkeypatch.setattr(
        login.api,
        "login",
        Mock(return_value={"success": False, "error": "reviewer_account"}),
    )

    login.render()

    assert ui.warnings == [
        "This is a reviewer account. Please use the reviewer login."
    ]


def test_signup_validates_before_calling_api(monkeypatch):
    ui = FakeStreamlit(
        {"Full Name": "", "Email": "invalid", "PIN": "12", "Confirm PIN": "34"},
        clicked_button="Create Account",
    )
    monkeypatch.setattr(signup, "st", ui)
    signup_api = Mock()
    monkeypatch.setattr(signup.api, "signup", signup_api)

    signup.render()

    signup_api.assert_not_called()
    assert len(ui.errors) >= 5


def test_signup_creates_account_and_returns_to_login(monkeypatch):
    ui = FakeStreamlit(
        {
            "Full Name": "Ava",
            "Email": "ava@example.com",
            "Phone (optional)": "",
            "PIN": "1234",
            "Confirm PIN": "1234",
            "Address Line 1": "1 Main St",
            "Address Line 2 (optional)": "",
            "City": "Town",
            "State": "State",
            "Pincode": "123456",
        },
        clicked_button="Create Account",
    )
    monkeypatch.setattr(signup, "st", ui)
    monkeypatch.setattr(session, "st", ui)
    signup_api = Mock(return_value={"success": True, "message": "Account created"})
    monkeypatch.setattr(signup.api, "signup", signup_api)

    signup.render()

    signup_api.assert_called_once_with(
        "Ava",
        "ava@example.com",
        "",
        "1234",
        "1 Main St",
        "",
        "Town",
        "State",
        "123456",
    )
    assert ui.session_state["signup_success"] == "Account created"
    assert ui.successes == []
    assert ui.session_state["page"] == "login"
    assert ui.reruns == 1


def test_login_navigation_away_clears_signup_message(monkeypatch):
    for button, page in (
        ("New here? Create an account", "signup"),
        ("Reviewer? Login here", "reviewer"),
    ):
        ui = FakeStreamlit(clicked_button=button)
        ui.session_state["signup_success"] = "Account created."
        monkeypatch.setattr(login, "st", ui)

        login.render()

        assert ui.session_state["signup_success"] is None
        assert ui.session_state["page"] == page
