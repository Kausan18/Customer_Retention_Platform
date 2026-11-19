import pytest

from frontend.utils.detect_category import detect_category


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("WHAT IS THE REFUND POLICY?!", "policy"),
        ("Can I exchange this item?", "policy"),
        ("I want a refund or exchange for my headphones", "refund"),
        ("Please replace my jacket", "exchange"),
        ("Where is my package?", "order_tracking"),
        ("What is my payment status?", "order_status"),
        ("Buy shoes", "order_placement"),
    ],
)
def test_category_priority_and_keywords(message, expected):
    assert detect_category(message) == expected


@pytest.mark.parametrize(
    "message",
    ["yes", "confirm", "that one", "first option", "option 1", "I'll take it", "sounds good"],
)
def test_confirmations_route_to_order_placement(message):
    assert detect_category(message) == "order_placement"


def test_unmatched_message_defaults_to_order_placement():
    assert detect_category("Could you help me?") == "order_placement"
