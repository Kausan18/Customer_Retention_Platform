from unittest.mock import Mock

from frontend import app


class FakeStreamlit:
    def __init__(self):
        self.session_state = {}
        self.page_config = None
        self.reruns = 0

    def set_page_config(self, **kwargs):
        self.page_config = kwargs

    def rerun(self):
        self.reruns += 1


def _set_ui(monkeypatch, page=None):
    ui = FakeStreamlit()
    if page is not None:
        ui.session_state["page"] = page
    monkeypatch.setattr(app, "st", ui)
    monkeypatch.setattr(app.session, "st", ui)
    return ui


def test_default_route_sets_page_config_and_renders_login(monkeypatch):
    ui = _set_ui(monkeypatch)
    render_login = Mock()
    monkeypatch.setattr(app.login, "render", render_login)

    app.main()

    assert ui.session_state["page"] == "login"
    assert ui.page_config == {
        "page_title": "TechMart Support",
        "page_icon": "🛒",
        "layout": "wide",
        "initial_sidebar_state": "collapsed",
    }
    render_login.assert_called_once_with()


def test_chat_route_redirects_unauthenticated_user(monkeypatch):
    ui = _set_ui(monkeypatch, page="chat")
    monkeypatch.setattr(app.session, "is_logged_in", lambda: False)
    render_chat = Mock()
    monkeypatch.setattr(app.chat, "render", render_chat)

    app.main()

    assert ui.session_state["page"] == "login"
    assert ui.reruns == 1
    render_chat.assert_not_called()


def test_chat_route_redirects_reviewer_to_reviewer_page(monkeypatch):
    ui = _set_ui(monkeypatch, page="chat")
    monkeypatch.setattr(app.session, "is_logged_in", lambda: True)
    monkeypatch.setattr(app.session, "get_role", lambda: "reviewer")
    render_chat = Mock()
    render_reviewer = Mock()
    monkeypatch.setattr(app.chat, "render", render_chat)
    monkeypatch.setattr(app.reviewer, "render", render_reviewer)

    app.main()

    assert ui.session_state["page"] == "reviewer"
    assert ui.reruns == 1
    render_chat.assert_not_called()


def test_payment_route_requires_login(monkeypatch):
    ui = _set_ui(monkeypatch, page="payment")
    monkeypatch.setattr(app.session, "is_logged_in", lambda: False)
    render_payment = Mock()
    monkeypatch.setattr(app.payment, "render", render_payment)

    app.main()

    assert ui.session_state["page"] == "login"
    assert ui.reruns == 1
    render_payment.assert_not_called()
