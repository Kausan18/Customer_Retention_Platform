import json
from unittest.mock import patch

import pytest

from app.agents import router


def test_classify_intent_sends_prompt_and_returns_valid_intent():
    response = json.dumps({"intent": "refund", "confidence": 0.97})
    with patch.object(router, "call_gemini", return_value=response) as call_gemini:
        intent = router.classify_intent("I want a refund")

    assert intent == "refund"
    prompt = call_gemini.call_args.args[0]
    assert 'Classify this message: "I want a refund"' in prompt
    assert "Return ONLY valid JSON" in prompt
    assert '"exchange" takes priority over "order_placement"' in prompt
    assert '"policy" is for informational questions only' in prompt


def test_classify_intent_strips_markdown_fences():
    with patch.object(
        router,
        "call_gemini",
        return_value='```json\n{"intent": "greeting", "confidence": 0.9}\n```',
    ):
        assert router.classify_intent("Hi") == "greeting"


@pytest.mark.parametrize(
    "response",
    [
        None,
        "not json",
        "```json\nnot json\n```",
        "[]",
        '{"intent": "not-a-category", "confidence": 0.9}',
        '{"confidence": 0.9}',
    ],
)
def test_classify_intent_returns_unknown_for_invalid_response(response):
    with patch.object(router, "call_gemini", return_value=response):
        assert router.classify_intent("some request") == "unknown"
