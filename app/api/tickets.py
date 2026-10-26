# app/api/tickets.py
"""
POST /tickets       — create a support ticket
POST /tickets/{id}/runs — start a resolution workflow (stubbed for Checkpoint 3)
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, status

from app.agents.order_status import order_status_graph
from app.agents.order_tracking import order_tracking_graph
from app.auth.session import require_session
from app.db.database import execute, fetch_one
from app.schemas.ticket import TicketCreate, TicketResponse
from app.schemas.workflow_state import WorkflowRunResponse
from app.services import audit

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# POST /tickets
# ---------------------------------------------------------------------------

@router.post("", response_model=TicketResponse, status_code=status.HTTP_201_CREATED)
def create_ticket(
    body: TicketCreate,
    customer_id: str = Depends(require_session),
) -> TicketResponse:
    """
    Create a support ticket for the authenticated customer.
    customer_id is always derived from the session token — never from the body.
    """
    # If order_id provided, verify the order belongs to this customer
    if body.order_id:
        order = fetch_one(
            "SELECT id FROM orders WHERE id = ? AND customer_id = ?",
            (body.order_id, customer_id),
        )
        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found or does not belong to this customer.",
            )

    ticket_id = f"tkt_{uuid.uuid4().hex[:8]}"
    now = _now_iso()

    execute(
        """
        INSERT INTO tickets (id, customer_id, order_id, category, status, summary, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'open', ?, ?, ?)
        """,
        (ticket_id, customer_id, body.order_id, body.category, body.message, now, now),
    )

    audit.log_event(
        run_id=None,
        actor=customer_id,
        event_type="ticket_created",
        payload={"ticket_id": ticket_id, "category": body.category},
    )

    return TicketResponse(
        ticket_id=ticket_id,
        customer_id=customer_id,
        order_id=body.order_id,
        category=body.category,
        status="open",
        created_at=now,
    )


# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/runs
# ---------------------------------------------------------------------------

def run_workflow(ticket: dict, run_id: str, customer_id: str) -> dict | None:
    """
    Dispatches to the correct LangGraph workflow based on ticket category.
    order_tracking and order_status run synchronously via graph.invoke()
    and complete within the request. refund and exchange still use the
    Week 1 stub until Day 4 replaces it with interrupt/resume graphs.
    """
    category = ticket["category"]

    if category in ("order_tracking", "order_status"):
        initial_state = {
            "customer_id": customer_id,
            "ticket_id": ticket["id"],
            "run_id": run_id,
            "customer_message": ticket["summary"],
            "order_id": ticket.get("order_id"),
            "order": None,
            "response": None,
            "error": None,
            "final_status": None,
        }

        if category == "order_tracking":
            initial_state["shipment"] = None
            result_state = order_tracking_graph.invoke(
                initial_state,
                config={"configurable": {"thread_id": run_id}},
            )
        else:
            initial_state["payment"] = None
            result_state = order_status_graph.invoke(
                initial_state,
                config={"configurable": {"thread_id": run_id}},
            )

        final_status = result_state.get("final_status", "escalated")
        run_status = "completed" if final_status == "resolved" else "failed"

        execute(
            "UPDATE workflow_runs SET status = ?, completed_at = ? WHERE id = ?",
            (run_status, datetime.now(timezone.utc).isoformat(), run_id),
        )

        return {
            "run_status": run_status,
            "final_response": result_state.get("response"),
        }

    # The stub signature takes run_id, ticket, and the proposal timestamp.
    _create_stub_proposal(run_id, ticket, _now_iso())
    return None


@router.post(
    "/{ticket_id}/runs",
    response_model=WorkflowRunResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_run(
    ticket_id: str,
    customer_id: str = Depends(require_session),
) -> WorkflowRunResponse:
    """
    Start a resolution workflow for a ticket.

    Ownership check: ticket must belong to the authenticated customer.

    For Checkpoint 3, the LangGraph workflow is STUBBED:
    - Creates a workflow_run row with status=running
    - Creates a stub action_proposal + approval row so the approval
      endpoints can be tested end-to-end
    - Week 2 replaces the stub block with actual LangGraph dispatch
    """
    # Ownership check
    ticket = fetch_one(
        "SELECT * FROM tickets WHERE id = ? AND customer_id = ?",
        (ticket_id, customer_id),
    )
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found or does not belong to this customer.",
        )

    if ticket["status"] not in ("open", "in_progress"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ticket is already in status '{ticket['status']}'.",
        )

    run_id = f"run_{uuid.uuid4().hex[:8]}"
    thread_id = str(uuid.uuid4())
    now = _now_iso()

    execute(
        """
        INSERT INTO workflow_runs (id, ticket_id, thread_id, status, started_at)
        VALUES (?, ?, ?, 'running', ?)
        """,
        (run_id, ticket_id, thread_id, now),
    )

    execute(
        "UPDATE tickets SET status = 'in_progress', updated_at = ? WHERE id = ?",
        (now, ticket_id),
    )

    audit.log_event(
        run_id=run_id,
        actor=customer_id,
        event_type="workflow_started",
        payload={"ticket_id": ticket_id, "category": ticket["category"]},
    )

    run_workflow(ticket, run_id, customer_id)

    return WorkflowRunResponse(
        run_id=run_id,
        ticket_id=ticket_id,
        thread_id=thread_id,
        status="running",
        started_at=now,
    )


def _create_stub_proposal(run_id: str, ticket: dict, now: str) -> None:
    """
    Checkpoint 3 stub: create a pending proposal + approval record.
    Only called for refund/exchange categories — other categories get no proposal.
    The proposal payload mirrors what the real LangGraph refund node will produce.
    """
    if ticket["category"] not in ("refund", "exchange"):
        return

    proposal_id = f"prp_{uuid.uuid4().hex[:8]}"
    approval_id = f"apr_{uuid.uuid4().hex[:8]}"
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=24)
    ).isoformat().replace("+00:00", "Z")

    # Build a realistic payload
    payload = {
        "action": f"request_{ticket['category']}_approval",
        "order_id": ticket["order_id"] or "ord_stub",
        "amount_minor": 1409500,  # ₹14,095 — matches ord_0167 for Checkpoint 3 demo
        "currency": "INR",
        "reason": "Checkpoint 3 stub — return received and inspection passed",
        "evidence_refs": ["stub_return_001"],
        "policy_refs": ["refund_policy_v1:section_2"],
        "missing_information": [],
    }
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()

    execute(
        """
        INSERT INTO action_proposals (id, run_id, action_type, payload, payload_hash, status, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (proposal_id, run_id, ticket["category"], payload_json, payload_hash, now, expires_at),
    )

    execute(
        """
        INSERT INTO approvals (id, proposal_id, reviewer_id, decision, decided_at, created_at)
        VALUES (?, ?, 'pending_reviewer', NULL, NULL, ?)
        """,
        (approval_id, proposal_id, now),
    )

    # Pause the run waiting for approval
    execute(
        "UPDATE workflow_runs SET status = 'paused' WHERE id = ?",
        (run_id,),
    )