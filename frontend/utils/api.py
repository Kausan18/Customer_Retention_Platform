"""Centralize HTTP calls from the frontend to the FastAPI backend."""

import os
import time

import requests

from .session import get_customer_id


BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _response_result(response, list_key: str = "data") -> dict:
    if not 200 <= response.status_code < 300:
        payload = response.json()
        error = (
            payload.get("detail", "Unknown error")
            if isinstance(payload, dict)
            else "Unknown error"
        )
        return {"success": False, "error": error}

    payload = response.json()
    if isinstance(payload, dict):
        return {"success": True, **payload}
    return {"success": True, list_key: payload}


def login(customer_id: str, pin: str) -> dict:
    response = requests.post(
        f"{BASE_URL}/auth/login",
        json={"customer_id": customer_id, "pin": pin},
    )
    if response.status_code == 403:
        return {"success": False, "error": "reviewer_account"}
    if not 200 <= response.status_code < 300:
        return _response_result(response)

    payload = response.json()
    return {
        "success": True,
        "token": payload["token"],
        "customer_id": payload["customer_id"],
        "name": payload["name"],
        "role": "customer",
    }


def reviewer_login(customer_id: str, pin: str) -> dict:
    response = requests.post(
        f"{BASE_URL}/auth/reviewer-login",
        json={"customer_id": customer_id, "pin": pin},
    )
    if not 200 <= response.status_code < 300:
        return _response_result(response)

    payload = response.json()
    return {
        "success": True,
        "token": payload["token"],
        "reviewer_id": payload["reviewer_id"],
        "name": payload["name"],
        "role": "reviewer",
    }


def signup(
    name: str,
    email: str,
    phone: str,
    pin: str,
    address_line1: str,
    address_line2: str,
    city: str,
    state: str,
    pincode: str,
) -> dict:
    response = requests.post(
        f"{BASE_URL}/auth/signup",
        json={
            "name": name,
            "email": email,
            "phone": phone,
            "pin": pin,
            "address_line1": address_line1,
            "address_line2": address_line2,
            "city": city,
            "state": state,
            "pincode": pincode,
        },
    )
    return _response_result(response)


def logout(token: str) -> None:
    requests.post(
        f"{BASE_URL}/auth/logout",
        headers=_auth_headers(token),
    )


def create_ticket(
    token: str,
    category: str,
    message: str,
    order_id: str | None = None,
) -> dict:
    body = {"category": category, "message": message}
    if order_id is not None:
        body["order_id"] = order_id
    response = requests.post(
        f"{BASE_URL}/tickets",
        headers=_auth_headers(token),
        json=body,
    )
    return _response_result(response)


def start_run(token: str, ticket_id: str, message: str | None = None) -> dict:
    request_kwargs = {"headers": _auth_headers(token)}
    if message is not None:
        request_kwargs["json"] = {"message": message}
    response = requests.post(
        f"{BASE_URL}/tickets/{ticket_id}/runs",
        **request_kwargs,
    )
    return _response_result(response)


def send_message(token: str, run_id: str, message: str) -> dict:
    response = requests.post(
        f"{BASE_URL}/runs/{run_id}/messages",
        headers=_auth_headers(token),
        json={"message": message},
    )
    return _response_result(response)


def get_run(token: str, run_id: str) -> dict:
    response = requests.get(
        f"{BASE_URL}/runs/{run_id}",
        headers=_auth_headers(token),
    )
    return _response_result(response)


def poll_run(
    token: str,
    run_id: str,
    max_attempts: int = 20,
    interval: float = 1.5,
) -> dict:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    result = {}
    for attempt in range(max_attempts):
        result = get_run(token, run_id)
        if not result.get("success") or result.get("status") in {
            "completed",
            "failed",
            "paused",
        }:
            return result
        if attempt + 1 < max_attempts:
            time.sleep(interval)
    return result


def get_run_events(token: str, run_id: str) -> dict:
    response = requests.get(
        f"{BASE_URL}/runs/{run_id}/events",
        headers=_auth_headers(token),
    )
    return _response_result(response, list_key="events")


def get_approvals(token: str) -> dict:
    response = requests.get(
        f"{BASE_URL}/approvals",
        headers=_auth_headers(token),
    )
    return _response_result(response, list_key="approvals")


def submit_decision(token: str, approval_id: str, decision: str) -> dict:
    response = requests.post(
        f"{BASE_URL}/approvals/{approval_id}/decision",
        headers=_auth_headers(token),
        json={"decision": decision, "reviewer_id": get_customer_id()},
    )
    return _response_result(response)


def validate_cart(token: str, items: list) -> dict:
    response = requests.post(
        f"{BASE_URL}/cart/validate",
        headers=_auth_headers(token),
        json={"items": items},
    )
    return _response_result(response)


def place_order(token: str, items: list, payment_method: str) -> dict:
    response = requests.post(
        f"{BASE_URL}/orders",
        headers=_auth_headers(token),
        json={"items": items, "payment_method": payment_method},
    )
    return _response_result(response)
