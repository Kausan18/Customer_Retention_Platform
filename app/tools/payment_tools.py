"""
Payment tools: proposal creation and approved refund execution.

Design:
- create_refund_proposal writes to action_proposals + approvals. Idempotent.
- execute_approved_refund is gated by tool_authorizer — cannot run without
  passing all 6 authorization checks. Uses a stable idempotency key so
  retrying with the same proposal never double-charges.
"""
import hashlib
import json
import secrets
from datetime import datetime, timezone, timedelta

from app.db.database import fetch_one, execute
from app.guardrails.tool_authorizer import check_authorization
from app.services.audit import log_event
from mock_services.payment_service import refund as payment_refund


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_refund_proposal(
    run_id: str,
    order_id: str,
    amount_minor: int,
    reason: str,
    policy_refs: list,
    evidence_refs: list,
) -> dict:
    """
    Create an action_proposals row and a linked approvals row.
    Does NOT touch the payment service. Idempotent: returns existing proposal
    if one already exists for this run_id + action_type.
    """
    existing = fetch_one(
        "SELECT id, payload, status FROM action_proposals "
        "WHERE run_id = ? AND action_type = 'refund' LIMIT 1",
        (run_id,),
    )
    if existing:
        return {
            "success": True,
            "proposal_id": existing["id"],
            "status": existing["status"],
            "already_existed": True,
        }

    proposal_id = f"prp_{secrets.token_hex(4)}"
    approval_id = f"apr_{secrets.token_hex(4)}"
    now = _utcnow()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    payload = {
        "action_type": "refund",
        "order_id": order_id,
        "amount_minor": amount_minor,
        "reason": reason,
        "policy_refs": policy_refs,
        "evidence_refs": evidence_refs,
    }
    payload_json = json.dumps(payload, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()

    execute(
        "INSERT INTO action_proposals "
        "(id, run_id, action_type, payload, payload_hash, status, created_at, expires_at) "
        "VALUES (?, ?, 'refund', ?, ?, 'pending', ?, ?)",
        (proposal_id, run_id, payload_json, payload_hash, now, expires_at),
    )
    execute(
        "INSERT INTO approvals (id, proposal_id, reviewer_id, decision, decided_at, created_at) "
        "VALUES (?, ?, 'pending_reviewer', NULL, NULL, ?)",
        (approval_id, proposal_id, now),
    )

    log_event(
        run_id,
        "system",
        "proposal_created",
        json.dumps({"proposal_id": proposal_id, "action_type": "refund"}),
    )

    return {
        "success": True,
        "proposal_id": proposal_id,
        "approval_id": approval_id,
        "amount_minor": amount_minor,
        "expires_at": expires_at,
        "already_existed": False,
    }


def execute_approved_refund(proposal_id: str, customer_id: str) -> dict:
    """
    Execute an approved refund. Runs tool_authorizer before any payment call.
    Idempotency key is stable — retrying with the same proposal_id is safe.
    customer_id is used for audit attribution only, not for authorization.
    """
    auth = check_authorization(proposal_id)
    if not auth.authorized:
        log_event(
            None,
            customer_id,
            "refund_authorization_failed",
            json.dumps({"proposal_id": proposal_id, "error_code": auth.error_code}),
        )
        return {
            "success": False,
            "error_code": auth.error_code,
            "retryable": False,
            "message": f"Authorization check failed: {auth.error_code}",
        }

    proposal = auth.proposal
    payload = json.loads(proposal["payload"])
    order_id = payload["order_id"]
    amount_minor = payload["amount_minor"]
    run_id = proposal["run_id"]

    # Stable key — same proposal_id always produces the same key
    idempotency_key = f"refund_{proposal_id}"

    result = payment_refund(order_id, amount_minor, idempotency_key)

    if result.get("success"):
        log_event(
            run_id,
            customer_id,
            "refund_executed",
            json.dumps(
                {
                    "proposal_id": proposal_id,
                    "idempotency_key": idempotency_key,
                    "status": result.get("status"),
                }
            ),
        )
    else:
        log_event(
            run_id,
            customer_id,
            "refund_failed",
            json.dumps(
                {"proposal_id": proposal_id, "error_code": result.get("error_code")}
            ),
        )

    return result