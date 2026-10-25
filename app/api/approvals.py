# app/api/approvals.py
"""
GET  /approvals                      — reviewer: list pending proposals
POST /approvals/{approval_id}/decision — reviewer: approve or reject
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, status

from app.db.database import execute, fetch_one, fetch_all
from app.schemas.approval import ApprovalDecision, ApprovalResponse
from app.services import audit

router = APIRouter(prefix="/approvals", tags=["approvals"])

# ---------------------------------------------------------------------------
# Reviewer authentication helper
# ---------------------------------------------------------------------------

def _require_reviewer(x_reviewer_pin: str = Header(..., alias="X-Reviewer-Pin")) -> str:
    """
    Simple reviewer auth via X-Reviewer-Pin header.
    In V1, the PIN is stored in the environment and shared among reviewers.
    Returns the pin value for use as reviewer identity in audit logs.
    """
    import os
    expected = os.environ.get("REVIEWER_PIN", "9999")
    # constant-time compare
    import hmac
    if not hmac.compare_digest(x_reviewer_pin.strip(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid reviewer PIN.",
        )
    return x_reviewer_pin.strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# GET /approvals
# ---------------------------------------------------------------------------

@router.get("")
def list_pending_approvals(
    _reviewer: str = Header(None, alias="X-Reviewer-Pin"),
) -> list[dict]:
    """
    Return all non-expired pending proposals with ticket + customer context.
    The reviewer sees everything they need to make a decision.
    """
    # Validate reviewer PIN (reuse helper but allow header directly here)
    import os, hmac
    expected = os.environ.get("REVIEWER_PIN", "9999")
    if not _reviewer or not hmac.compare_digest(_reviewer.strip(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid reviewer PIN.",
        )

    now = _now_iso()
    rows = fetch_all(
        """
        SELECT
            ap.id            AS proposal_id,
            ap.run_id,
            ap.action_type,
            ap.payload,
            ap.status        AS proposal_status,
            ap.created_at    AS proposal_created_at,
            ap.expires_at,
            apr.id           AS approval_id,
            t.id             AS ticket_id,
            t.customer_id,
            t.order_id,
            t.category,
            t.summary
        FROM action_proposals ap
        JOIN approvals apr        ON apr.proposal_id = ap.id
        JOIN workflow_runs wr     ON wr.id = ap.run_id
        JOIN tickets t            ON t.id = wr.ticket_id
        WHERE ap.status = 'pending'
          AND ap.expires_at > ?
        ORDER BY ap.created_at ASC
        """,
        (now,),
    )

    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        result.append({
            "proposal_id": row["proposal_id"],
            "approval_id": row["approval_id"],
            "run_id": row["run_id"],
            "action_type": row["action_type"],
            "payload": payload,
            "expires_at": row["expires_at"],
            "ticket": {
                "ticket_id": row["ticket_id"],
                "customer_id": row["customer_id"],
                "order_id": row["order_id"],
                "category": row["category"],
                "summary": row["summary"],
            },
        })
    return result


# ---------------------------------------------------------------------------
# POST /approvals/{approval_id}/decision
# ---------------------------------------------------------------------------

@router.post("/{approval_id}/decision", response_model=ApprovalResponse)
def submit_decision(
    approval_id: str,
    body: ApprovalDecision,
    reviewer: str = Header(..., alias="X-Reviewer-Pin"),  # validated below
) -> ApprovalResponse:
    """
    Reviewer approves or rejects a proposal.

    Checks (in order):
      1. Reviewer PIN valid
      2. Approval record exists and is undecided
      3. Linked proposal is still pending and not expired
      4. Payload hash matches (tamper detection)
    """
    # 1. Reviewer auth
    import os, hmac
    expected = os.environ.get("REVIEWER_PIN", "9999")
    if not hmac.compare_digest(reviewer.strip(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid reviewer PIN.",
        )

    # 2. Approval record
    approval = fetch_one(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    )
    if approval is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Approval record not found.",
        )
    if approval["decision"] is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This approval has already been decided: {approval['decision']}.",
        )

    # 3. Linked proposal
    proposal = fetch_one(
        "SELECT * FROM action_proposals WHERE id = ?",
        (approval["proposal_id"],),
    )
    if proposal is None or proposal["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Linked proposal is no longer pending.",
        )

    now = _now_iso()
    now_dt = datetime.now(timezone.utc)
    expires_dt = datetime.fromisoformat(
        proposal["expires_at"].replace("Z", "+00:00")
    )
    if now_dt > expires_dt:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Proposal has expired. Customer must resubmit.",
        )

    # 4. Hash check
    try:
        current_payload = json.loads(proposal["payload"])
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not parse proposal payload.",
        )

    canonical = json.dumps(current_payload, sort_keys=True, separators=(",", ":"))
    computed_hash = hashlib.sha256(canonical.encode()).hexdigest()
    if computed_hash != proposal["payload_hash"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Proposal payload has been tampered with. Approval rejected.",
        )

    # All checks passed — persist the decision
    execute(
        """
        UPDATE approvals
        SET reviewer_id = ?, decision = ?, decided_at = ?
        WHERE id = ?
        """,
        (body.reviewer_id, body.decision, now, approval_id),
    )

    new_proposal_status = "approved" if body.decision == "approved" else "rejected"
    execute(
        "UPDATE action_proposals SET status = ? WHERE id = ?",
        (new_proposal_status, proposal["id"]),
    )

    # Update workflow run status
    if body.decision == "approved":
        # Signal LangGraph resume (Week 2 wires the actual resume call here)
        execute(
            "UPDATE workflow_runs SET status = 'running' WHERE id = ?",
            (proposal["run_id"],),
        )
        # For Checkpoint 3 stub: mark completed immediately since no real graph
        execute(
            "UPDATE workflow_runs SET status = 'completed', completed_at = ? WHERE id = ?",
            (now, proposal["run_id"]),
        )
    else:
        execute(
            "UPDATE workflow_runs SET status = 'completed', completed_at = ? WHERE id = ?",
            (now, proposal["run_id"]),
        )

    audit.log_event(
        run_id=proposal["run_id"],
        actor=body.reviewer_id,
        event_type=f"proposal_{body.decision}",
        payload={
            "proposal_id": proposal["id"],
            "approval_id": approval_id,
            "action_type": proposal["action_type"],
        },
    )

    return ApprovalResponse(
        approval_id=approval_id,
        proposal_id=proposal["id"],
        decision=body.decision,
        reviewer_id=body.reviewer_id,
        decided_at=now,
        run_status="completed",
    )