import pytest

from frontend.utils.detect_category import detect_category


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("WHAT IS THE REFUND POLICY?!", "policy"),
        ("Can I exchange this item?", "policy"),
        ("What is the refund policy for ord_12345?", "refund"),
        ("I want a refund and want to buy a jacket", "refund"),
        ("I want to exchange my headphones", "exchange"),
        ("Please replace my jacket", "exchange"),
        ("I need a replacement for this broken device", "damaged"),
        ("The wrong item arrived", "damaged"),
        ("Where is my package?", "order_tracking"),
        ("What is my payment status?", "order_status"),
        ("Buy shoes", "order_placement"),
        ("Could you help me?", "unknown"),
    ],
)
def test_category_priority_and_keywords(message, expected):
    assert detect_category(message) == expected


@pytest.mark.parametrize(
    "message",
    ["yes", "confirm", "that one", "first option", "option 1", "I'll take it", "sounds good"],
)
def test_confirmations_fall_back_to_unknown(message):
    assert detect_category(message) == "unknown"


def test_policy_is_skipped_when_message_has_order_reference():
    assert detect_category("What is the policy for ord_12345?") == "unknown"


def test_exchange_beats_order_placement():
    cases = [
        ("i want to apply for an exchange for my order on shoes", "exchange"),
        ("i want to exchange my headphones", "exchange"),
        ("i want to swap my order for another one", "exchange"),
        ("exchange my item please", "exchange"),
    ]
    for message, expected in cases:
        assert detect_category(message) == expected


def test_refund_beats_order_placement():
    cases = [
        ("i want a refund for my order", "refund"),
        ("give me my money back", "refund"),
        ("i need a reimbursement", "refund"),
    ]
    for message, expected in cases:
        assert detect_category(message) == expected


def test_order_placement_matches_browse_intent():
    cases = [
        ("i want to buy wireless headphones", "order_placement"),
        ("show me bluetooth speakers", "order_placement"),
        ("browse shoes under 2000", "order_placement"),
        ("i'd like to get a smartwatch", "order_placement"),
    ]
    for message, expected in cases:
        assert detect_category(message) == expected


def test_tracking_and_status():
    cases = [
        ("where is my order", "order_tracking"),
        ("track my shipment", "order_tracking"),
        ("is my payment confirmed", "order_status"),
    ]
    for message, expected in cases:
        assert detect_category(message) == expected
