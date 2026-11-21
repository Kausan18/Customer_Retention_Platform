# app/api/tickets.py
"""
POST /tickets       — create a support ticket
POST /tickets/{id}/runs — start a resolution workflow
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.agents.order_status import order_status_graph
from app.agents.order_tracking import order_tracking_graph
from app.auth.session import require_session
from app.db.database import execute, fetch_one
from app.schemas.ticket import TicketCreate, TicketResponse
from app.schemas.workflow_state import WorkflowRunResponse
from app.services import audit
from app.guardrails.input_screener import inspect_request, GuardrailDecision
from app.guardrails.intent_validator import validate_intent
from app.services.rate_limiter import rate_limiter

from langgraph.types import Command  # noqa: F401 — imported here for clarity
from app.agents.refund_workflow import refund_workflow_graph
from app.agents.exchange_workflow import exchange_workflow_graph
from app.agents.order_placement import order_placement_graph

router = APIRouter(prefix="/tickets", tags=["tickets"])
logger = logging.getLogger(__name__)


class WorkflowRunRequest(BaseModel):
    message: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


POLICY_QUESTION_KEYWORDS = (
    "how can i",
    "how do i",
    "how to",
    "what is your",
    "what are your",
    "tell me about",
    "can you explain",
    "do you offer",
    "policy",
    "refund policy",
    "exchange policy",
    "return policy",
    "how long",
    "how many days",
    "eligible",
    "damaged",
    "shipping delay",
    "rules",
    "terms",
    "process",
    "procedure",
    "window",
)


_REFUND_ACTION_PHRASES = (
    "return my order",
    "want to return",
    "can i return",
    "i want a refund",
    "want a refund",
    "get a refund",
    "need a refund",
)

def _is_policy_question(message: str) -> bool:
    message_lower = message.lower()
    if any(phrase in message_lower for phrase in _REFUND_ACTION_PHRASES):
        return False
    return any(keyword in message_lower for keyword in POLICY_QUESTION_KEYWORDS)


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
    run_data = fetch_one(
        "SELECT thread_id FROM workflow_runs WHERE id = ? AND ticket_id = ?",
        (run_id, ticket_id),
    )
    if run_data is None:
        raise RuntimeError(f"Workflow run {run_id} was not found for ticket {ticket_id}.")

    config = {"configurable": {"thread_id": run_data["thread_id"]}}

    try:
        result = graph.invoke(initial_state, config=config)
        print(f"[DEBUG graph] error_code={result.get('error_code')} error_message={result.get('error_message')} final_response={result.get('final_response')}")
    except Exception as exc:
        import traceback
        traceback.print_exc()
        completed_at = datetime.now(timezone.utc).isoformat()
        execute(
            "UPDATE workflow_runs SET status = 'failed', completed_at = ? WHERE id = ?",
            (completed_at, run_id),
        )
        execute(
            "UPDATE tickets SET summary = ?, status = 'escalated', updated_at = ? WHERE id = ?",
            (f"Workflow error: {exc}", completed_at, ticket_id),
        )
        audit.log_event(
            run_id=run_id,
            actor="system",
            event_type="workflow_exception",
            payload={"error": str(exc)},
        )
        return

    graph_state = graph.get_state(config)
    final_response = result.get("final_response") or ""
    error_code = result.get("error_code")
    completed_at = datetime.now(timezone.utc).isoformat()

    if graph_state.next:
        # Graph paused at interrupt — waiting for reviewer
        execute(
            "UPDATE workflow_runs SET status = 'paused' WHERE id = ?",
            (run_id,),
        )
        execute(
            "UPDATE tickets SET summary = ?, updated_at = ? WHERE id = ?",
            (final_response, completed_at, ticket_id),
        )
    elif error_code:
        # Graph completed but hit a business rule error (ineligible, missing data etc)
        execute(
            "UPDATE workflow_runs SET status = 'failed', completed_at = ? WHERE id = ?",
            (completed_at, run_id),
        )
        execute(
            "UPDATE tickets SET summary = ?, status = 'resolved', updated_at = ? WHERE id = ?",
            (final_response, completed_at, ticket_id),
        )
    else:
        # Graph completed successfully
        execute(
            "UPDATE workflow_runs SET status = 'completed', completed_at = ? WHERE id = ?",
            (completed_at, run_id),
        )
        execute(
            "UPDATE tickets SET summary = ?, status = 'resolved', updated_at = ? WHERE id = ?",
            (final_response, completed_at, ticket_id),
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
    decision = inspect_request(body.message, customer_id)
    if decision.action == "BLOCK":
        raise HTTPException(
            status_code=400,
            detail={"blocked": True, "reason": decision.reason},
        )
    if not rate_limiter.check(customer_id):
        raise HTTPException(
            status_code=429,
            detail={"blocked": True, "reason": "RATE_LIMIT_EXCEEDED"},
        )

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

def run_workflow(
    ticket: dict,
    run_id: str,
    customer_id: str,
    thread_id: str,
) -> dict | None:
    """
    Dispatches to the correct LangGraph workflow based on ticket category.
    Policy questions and order lookups complete synchronously. Refund and
    exchange may pause while waiting for reviewer approval.
    """
    message = ticket.get("summary") or ticket.get("message", "")
    category = ticket["category"]
    if category != "policy" and _is_policy_question(message):
        from app.main import answer_policy_question

        final_response = answer_policy_question(message)
        completed_at = datetime.now(timezone.utc).isoformat()
        execute(
            "UPDATE tickets SET summary = ?, status = 'resolved', updated_at = ? WHERE id = ?",
            (final_response, completed_at, ticket["id"]),
        )
        execute(
            "UPDATE workflow_runs SET status = 'completed', completed_at = ? WHERE id = ?",
            (completed_at, run_id),
        )
        return {
            "run_status": "completed",
            "final_response": final_response,
        }

    if category == "policy":
        from app.main import answer_policy_question

        response = answer_policy_question(ticket.get("summary") or "")
        completed_at = _now_iso()
        execute(
            "UPDATE tickets SET status='resolved', summary=? WHERE id=?",
            (response, ticket["id"]),
        )
        execute(
            "UPDATE workflow_runs SET status='completed', completed_at=? WHERE id=?",
            (completed_at, run_id),
        )
        audit.log_event(
            run_id=run_id,
            actor=customer_id,
            event_type="workflow_completed",
            payload={"category": "policy"},
        )
        return {
            "status": "completed",
            "final_response": response,
        }

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
        config = {"configurable": {"thread_id": thread_id}}
        try:
            result = order_placement_graph.invoke(initial_state, config=config)
        except Exception as e:
            logger.exception("Order placement graph failed for run %s", run_id)
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
                payload={"error_type": type(e).__name__},
            )
            raise HTTPException(
                status_code=500,
                detail="Order placement workflow failed.",
            ) from e

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
    body: WorkflowRunRequest | None = None,
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
    if ticket["category"] == "damaged":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Damaged-item tickets are handled by support without a workflow.",
        )

    request_message = body.message.strip() if body and body.message else None
    if request_message:
        decision = inspect_request(request_message, customer_id)
        if decision.action == "BLOCK":
            raise HTTPException(
                status_code=400,
                detail={"blocked": True, "reason": decision.reason},
            )
        run_message = decision.text
    else:
        run_message = ticket.get("summary") or ticket.get("message", "")

    policy_category = ticket["category"] == "policy"
    policy_question = (
        not policy_category and _is_policy_question(run_message)
    )
    if not policy_question and not policy_category:
        intent_result = validate_intent(ticket["category"], run_message)
        if intent_result.action == "BLOCK":
            raise HTTPException(
                status_code=400,
                detail={"blocked": True, "reason": intent_result.reason},
            )

    reopening_order_ticket = (
        ticket["category"] == "order_placement"
        and ticket["status"] == "resolved"
        and bool(request_message)
    )
    if ticket["status"] not in ("open", "in_progress") and not reopening_order_ticket:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ticket is already in status '{ticket['status']}'.",
        )

    if request_message:
        ticket_status = "open" if reopening_order_ticket else ticket["status"]
        execute(
            "UPDATE tickets SET summary = ?, status = ?, updated_at = ? WHERE id = ?",
            (run_message, ticket_status, _now_iso(), ticket_id),
        )
        ticket["summary"] = run_message
        ticket["status"] = ticket_status

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

    if policy_question:
        # Import locally because app.main imports this router during initialization.
        from app.main import answer_policy_question

        policy_answer = answer_policy_question(ticket["summary"] or "")
        completed_at = _now_iso()
        execute(
            "UPDATE tickets SET summary = ?, status = 'resolved', updated_at = ? WHERE id = ?",
            (policy_answer, completed_at, ticket["id"]),
        )
        execute(
            "UPDATE workflow_runs SET status = 'completed', completed_at = ? WHERE id = ?",
            (completed_at, run_id),
        )
        audit.log_event(
            run_id=run_id,
            actor=ticket["customer_id"],
            event_type="workflow_completed",
            payload={"method": "policy_rag"},
        )
        return WorkflowRunResponse(
            run_id=run_id,
            ticket_id=ticket["id"],
            thread_id=thread_id,
            status="completed",
            started_at=now,
            completed_at=completed_at,
            final_response=policy_answer,
        )

    workflow_result = run_workflow(ticket, run_id, customer_id, thread_id)
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
        final_response=(
            workflow_result.get("final_response")
            if policy_category and workflow_result
            else None
        ),
    )