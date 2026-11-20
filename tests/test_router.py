from unittest.mock import patch

from app.agents.router import classify_intent


def test_exchange_classified_correctly():
    with patch(
        "app.agents.router.call_gemini",
        return_value='{"intent": "exchange", "confidence": 0.95}',
    ):
        assert classify_intent("i want to exchange my headphones") == "exchange"


def test_refund_classified_correctly():
    with patch(
        "app.agents.router.call_gemini",
        return_value='{"intent": "refund", "confidence": 0.98}',
    ):
        assert classify_intent("i want a refund") == "refund"


def test_policy_classified_correctly():
    with patch(
        "app.agents.router.call_gemini",
        return_value='{"intent": "policy", "confidence": 0.9}',
    ):
        assert classify_intent("what is the refund window?") == "policy"


def test_invalid_intent_returns_unknown():
    with patch(
        "app.agents.router.call_gemini",
        return_value='{"intent": "grocery_shopping", "confidence": 0.5}',
    ):
        assert classify_intent("whatever") == "unknown"


def test_malformed_json_returns_unknown():
    with patch(
        "app.agents.router.call_gemini",
        return_value="not json at all",
    ):
        assert classify_intent("whatever") == "unknown"


def test_transactional_intents_set():
    from app.agents.router import TRANSACTIONAL_INTENTS

    assert "refund" in TRANSACTIONAL_INTENTS
    assert "exchange" in TRANSACTIONAL_INTENTS
    assert "policy" not in TRANSACTIONAL_INTENTS
