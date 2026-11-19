import json
import re
from types import SimpleNamespace
from unittest.mock import patch

from app.tools import catalog_tools, order_tools, payment_tools


def test_approved_refund_records_refund_and_payment_transaction():
    payload = {"order_id": "ord_test", "amount_minor": 12500}
    for service_result, expected_status in (
        ({"success": True, "provider_reference": "ref_provider"}, "success"),
        ({"success": False, "error_code": "PAYMENT_NOT_REFUNDABLE"}, "failed"),
    ):
        with (
            patch.object(
                payment_tools,
                "check_authorization",
                return_value=SimpleNamespace(
                    authorized=True,
                    proposal={
                        "payload": json.dumps(payload),
                        "run_id": "run_test",
                    },
                ),
            ),
            patch.object(payment_tools, "payment_refund", return_value=service_result) as refund,
            patch.object(payment_tools, "fetch_one", return_value={"id": "pay_test"}),
            patch.object(payment_tools, "execute_many") as execute_many,
            patch.object(payment_tools, "log_event"),
        ):
            result = payment_tools.execute_approved_refund("prp_test", "C000001")

        assert result is service_result
        refund.assert_called_once_with("ord_test", 12500, "refund_prp_test")
        statements = execute_many.call_args.args[0]
        assert len(statements) == 2
        refund_sql, refund_params = statements[0]
        transaction_sql, transaction_params = statements[1]
        assert "INSERT INTO refunds" in refund_sql
        assert "INSERT INTO payment_transactions" in transaction_sql
        assert re.fullmatch(r"ref_[0-9a-f]{8}", refund_params[0])
        assert refund_params[1:6] == (
            "pay_test", "ord_test", "prp_test", 12500, "refund_prp_test"
        )
        assert refund_params[6] == service_result.get("provider_reference")
        assert refund_params[7] == expected_status
        assert re.fullmatch(r"ptx_[0-9a-f]{8}", transaction_params[0])
        assert transaction_params[1:6] == (
            "pay_test", 12500, "refund_prp_test",
            service_result.get("provider_reference"), expected_status,
        )
        assert transaction_params[6] == refund_params[8]


def test_approved_exchange_records_parent_and_each_original_item():
    payload = {
        "original_order_id": "ord_original",
        "replacement_product_id": "prod_replacement",
        "replacement_price_minor": 25000,
        "replacement_product_name": "Replacement",
    }
    service_auth = SimpleNamespace(
        authorized=True,
        proposal={"payload": json.dumps(payload), "run_id": "run_test"},
    )
    with (
        patch.object(catalog_tools, "check_authorization", return_value=service_auth),
        patch.object(
            catalog_tools,
            "fetch_one",
            return_value={
                "id": "prod_replacement",
                "name": "Replacement",
                "category": "Shoes",
            },
        ),
        patch.object(
            catalog_tools,
            "fetch_all",
            return_value=[{"id": "item_original_1"}, {"id": "item_original_2"}],
        ) as fetch_items,
        patch.object(catalog_tools, "execute"),
        patch.object(catalog_tools, "execute_many") as execute_many,
        patch.object(catalog_tools, "log_event"),
    ):
        result = catalog_tools.execute_approved_exchange("prp_test", "C000001")

    assert result["success"] is True
    assert result["original_order_id"] == "ord_original"
    fetch_items.assert_called_once_with(
        "SELECT id FROM order_items WHERE order_id = ?", ("ord_original",)
    )
    statements = execute_many.call_args.args[0]
    assert len(statements) == 3
    exchange_id, original_order_id, replacement_order_id, customer_id, _created_at = (
        statements[0][1]
    )
    assert re.fullmatch(r"exc_[0-9a-f]{8}", exchange_id)
    assert original_order_id == "ord_original"
    assert replacement_order_id == result["new_order_id"]
    assert customer_id == "C000001"
    assert "'completed'" in statements[0][0]
    item_params = [params for _sql, params in statements[1:]]
    assert [params[2] for params in item_params] == [
        "item_original_1", "item_original_2"
    ]
    assert all(params[1] == exchange_id for params in item_params)
    assert all(params[3:] == ("prod_replacement",) for params in item_params)
    assert all(re.fullmatch(r"exi_[0-9a-f]{8}", params[0]) for params in item_params)


def test_get_return_status_includes_return_items_and_empty_list():
    return_row = {
        "id": "ret_test",
        "order_id": "ord_test",
        "status": "received",
        "reason": "Damaged",
        "received_at": "2026-01-01T00:00:00Z",
        "inspection_status": "pending",
    }
    items = [
        {
            "id": "reti_test",
            "return_id": "ret_test",
            "order_item_id": "item_test",
            "quantity": 1,
            "reason": "Damaged",
            "condition": "damaged",
        }
    ]
    with (
        patch.object(order_tools, "fetch_one", return_value=return_row),
        patch.object(order_tools, "fetch_all", return_value=items),
    ):
        result = order_tools.get_return_status("ord_test")
    assert result["items"] == items

    with (
        patch.object(order_tools, "fetch_one", return_value=return_row),
        patch.object(order_tools, "fetch_all", return_value=[]),
    ):
        result = order_tools.get_return_status("ord_test")
    assert result["items"] == []

    with patch.object(order_tools, "fetch_one", return_value=None):
        result = order_tools.get_return_status("ord_test")
    assert result["success"] is False
    assert "items" not in result
