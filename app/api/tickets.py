# app/api/tickets.py
"""
POST /tickets       — create a support ticket
POST /tickets/{id}/runs — start a resolution workflow
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.agents.order_status import order_status_graph
from app.agents.order_tracking import order_tracking_graph
from app.auth.session import require_session
from app.db.database import execute, fetch_one
from app.schemas.ticket import TicketCreate, TicketResponse
from app.schemas.workflow_state import WorkflowRunResponse
from app.services import audit

from langgraph.types import Command  # noqa: F401 — imported here for clarity
from app.agents.refund_workflow import refund_workflow_graph
from app.agents.exchange_workflow import exchange_workflow_graph
from app.agents.order_placement import order_placement_graph

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _build_initial_refund_state(ticket: dict, run_id: str, customer_id: str) -> dict:
    return {
        "run_id": run_id,
        "ticket_id": ticket["id"],
        "customer_id": customer_id,
        "order_id": ticket.get("order_id"),
        "complaint_message": ticket.get("summary") or "",
        "order": None,
        "payment": None,
        "return_record": None,
        "policy_passages": None,
        "eligibility_result": None,
        "proposal_id": None,
        "reviewer_decision": None,
        "execution_result": None,
        "error_code": None,
        "error_message": None,
        "final_response": None,
    }


def _build_initial_exchange_state(ticket: dict, run_id: str, customer_id: str) -> dict:
    return {
        "run_id": run_id,
        "ticket_id": ticket["id"],
        "customer_id": customer_id,
        "order_id": ticket.get("order_id"),
        "complaint_message": ticket.get("summary") or "",
        "order": None,
        "payment": None,
        "policy_passages": None,
        "eligibility_result": None,
        "catalog_results": None,
        "selected_replacement": None,
        "proposal_id": None,
        "reviewer_decision": None,
        "execution_result": None,
        "error_code": None,
        "error_message": None,
        "final_response": None,
    }


def _run_graph_and_update_status(
    graph, initial_state: dict, run_id: str, ticket_id: str
) -> None:
    """
    Invoke a LangGraph graph, check whether it paused at an interrupt or
    completed, and update workflow_runs.status accordingly.
    """
    run_data = fetch_one(
        "SELECT thread_id FROM workflow_runs WHERE id = ? AND ticket_id = ?",
        (run_id, ticket_id),
    )
    if run_data is None:
        raise RuntimeError(f"Workflow run {run_id} was not found for ticket {ticket_id}.")

    config = {"configurable": {"thread_id": run_data["thread_id"]}}

    try:
        result = graph.invoke(initial_state, config=config)
        graph_state = graph.get_state(config)

        if graph_state.next:
            execute(
                "UPDATE workflow_runs SET status = 'paused' WHERE id = ?",
                (run_id,),
            )
        else:
            error_code = result.get("error_code")
            final_status = "failed" if error_code else "completed"
            execute(
                "UPDATE workflow_runs SET status = ?, completed_at = ? WHERE id = ?",
                (final_status, datetime.now(timezone.utc).isoformat(), run_id),
            )
    except Exception as exc:
        execute(
            "UPDATE workflow_runs SET status = 'failed', completed_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), run_id),
        )
        audit.log_event(
            run_id=run_id,
            actor="system",
            event_type="workflow_exception",
            payload={"error": str(exc)},
        )


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
    and complete within the request. Refund and exchange may pause while
    waiting for reviewer approval.
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

    elif category == "refund":
        initial = _build_initial_refund_state(ticket, run_id, customer_id)
        _run_graph_and_update_status(refund_workflow_graph, initial, run_id, ticket["id"])

    elif category == "exchange":
        initial = _build_initial_exchange_state(ticket, run_id, customer_id)
        _run_graph_and_update_status(exchange_workflow_graph, initial, run_id, ticket["id"])

    elif ticket["category"] == "order_placement":
        initial_state = {
            "run_id": run_id,
            "ticket_id": ticket["id"],
            "customer_id": customer_id,
            "customer_message": ticket["summary"] or ticket.get("message", ""),
        }
        config = {"configurable": {"thread_id": run_id}}
        try:
            result = order_placement_graph.invoke(initial_state, config=config)
        except Exception as e:
            execute(
                "UPDATE workflow_runs SET status = 'failed' WHERE id = ?",
                (run_id,),
            )
            execute(
                "UPDATE tickets SET status = 'escalated' WHERE id = ?",
                (ticket["id"],),
            )
            audit.log_event(
                run_id=run_id,
                actor="system",
                event_type="workflow_error",
                payload=str(e),
            )
            raise HTTPException(status_code=500, detail=str(e))

        # The graph pauses at await_confirmation_node via interrupt().
        # LangGraph surfaces this as an __interrupt__ key in the result.
        if "__interrupt__" in result:
            print("Order placement interrupt payload:", result["__interrupt__"])
            execute("UPDATE workflow_runs SET status = 'paused' WHERE id = ?", (run_id,))
            interrupt_value = result["__interrupt__"][0].value
            options_message = (
                interrupt_value.get("message", "")
                if isinstance(interrupt_value, dict)
                else str(interrupt_value)
            )
            execute(
                "UPDATE tickets SET summary = ? WHERE id = ?",
                (options_message, ticket["id"]),
            )
        else:
            final_status = "failed" if result.get("error_code") else "completed"
            execute("UPDATE workflow_runs SET status = ? WHERE id = ?", (final_status, run_id))

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

    Creates a workflow run row and dispatches to the graph for the
    ticket's category.
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
    run_data = fetch_one(
        "SELECT status, completed_at FROM workflow_runs WHERE id = ?",
        (run_id,),
    )
    if run_data is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Workflow run not found after dispatch.",
        )

    return WorkflowRunResponse(
        run_id=run_id,
        ticket_id=ticket_id,
        thread_id=thread_id,
        status=run_data["status"],
        started_at=now,
        completed_at=run_data["completed_at"],
    )