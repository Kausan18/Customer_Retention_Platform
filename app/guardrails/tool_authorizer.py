# app/guardrails/tool_authorizer.py
"""
Guardrail 4: Tool Call Authorizer.

The last check before execute_approved_refund or execute_approved_exchange
calls the mock payment service. Verifies:
  1. Proposal exists
  2. Proposal status is 'approved'
  3. An approvals row exists with decision == 'approved'
  4. Approval is not expired
  5. Payload hash matches (tamper detection)

Returns AuthorizationResult. Callers must check .authorized before proceeding.
This check cannot be bypassed — it runs inside the execute_* tool functions,
not in the workflow graph layer.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from app.db.database import fetch_one


@dataclass
class AuthorizationResult:
    authorized: bool
    error_code: str | None = None
    message: str | None = None
    proposal: dict | None = None  # Populated on success for caller convenience


def _compute_hash(payload: dict) -> str:
    """Compute SHA256 of the canonical JSON representation of a payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def authorize_execution(proposal_id: str) -> AuthorizationResult:
    """
    Run all 5 authorization checks for a proposal before execution.

    Args:
        proposal_id: The proposal to authorize.

    Returns:
        AuthorizationResult with authorized=True and the proposal dict on success,
        or authorized=False with an error_code on any check failure.
    """
    # Check 1: Proposal exists
    proposal = fetch_one(
        "SELECT * FROM action_proposals WHERE id = ?",
        (proposal_id,),
    )
    if proposal is None:
        return AuthorizationResult(
            authorized=False,
            error_code="PROPOSAL_NOT_FOUND",
            message=f"No proposal found with id {proposal_id}.",
        )

    # Check 2: Proposal status must be 'approved'
    if proposal["status"] != "approved":
        return AuthorizationResult(
            authorized=False,
            error_code="PROPOSAL_NOT_APPROVED",
            message=(
                f"Proposal {proposal_id} has status '{proposal['status']}', "
                f"expected 'approved'."
            ),
        )

    # Check 3: An approvals row with decision == 'approved' must exist
    approval = fetch_one(
        "SELECT * FROM approvals WHERE proposal_id = ? AND decision = 'approved'",
        (proposal_id,),
    )
    if approval is None:
        return AuthorizationResult(
            authorized=False,
            error_code="NO_APPROVAL_RECORD",
            message=f"No approved approval record found for proposal {proposal_id}.",
        )

    # Check 4: decided_at must be present (reviewer actually acted)
    if not approval["decided_at"]:
        return AuthorizationResult(
            authorized=False,
            error_code="APPROVAL_NOT_DECIDED",
            message="Approval record exists but reviewer has not yet decided.",
        )

    # Check 5: Proposal must not be expired
    expires_at_str = proposal["expires_at"]
    try:
        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if now > expires_at:
            return AuthorizationResult(
                authorized=False,
                error_code="PROPOSAL_EXPIRED",
                message=f"Proposal {proposal_id} expired at {expires_at_str}.",
            )
    except (ValueError, AttributeError):
        return AuthorizationResult(
            authorized=False,
            error_code="INVALID_EXPIRY_FORMAT",
            message=f"Could not parse expires_at: {expires_at_str}.",
        )

    # Check 6: Payload hash must match (tamper detection)
    try:
        current_payload = json.loads(proposal["payload"])
    except (json.JSONDecodeError, TypeError):
        return AuthorizationResult(
            authorized=False,
            error_code="PAYLOAD_PARSE_ERROR",
            message="Could not parse proposal payload for hash verification.",
        )

    computed_hash = _compute_hash(current_payload)
    if computed_hash != proposal["payload_hash"]:
        return AuthorizationResult(
            authorized=False,
            error_code="PAYLOAD_TAMPERED",
            message=(
                "Proposal payload hash does not match stored hash. "
                "The proposal may have been modified after approval."
            ),
        )

    return AuthorizationResult(
        authorized=True,
        proposal={**current_payload, "proposal_id": proposal_id},
    )