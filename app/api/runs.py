# app/api/runs.py
"""
GET /runs/{run_id}         — poll workflow status
GET /runs/{run_id}/events  — audit event timeline (redacted)
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.session import require_session
from app.db.database import fetch_one, fetch_all
from app.schemas.workflow_state import WorkflowRunResponse, WorkflowEvent
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.auth.session import require_session
from app.db.database import fetch_one, execute
from app.guardrails.input_screener import inspect_request
from app.services.audit import log_event
from app.agents.order_placement import order_placement_graph
from langgraph.types import Command

router = APIRouter(prefix="/runs", tags=["runs"])


def _get_run_with_ownership(run_id: str, customer_id: str) -> dict:
    """Fetch a run and verify it belongs to the authenticated customer."""
    run = fetch_one(
        """
        SELECT
            wr.id,
            wr.ticket_id,
            wr.thread_id,
            wr.status,
            wr.started_at,
            wr.completed_at,
            t.summary AS final_response,
            t.customer_id AS ticket_customer_id
        FROM workflow_runs wr
        JOIN tickets t ON t.id = wr.ticket_id
        WHERE wr.id = ?
        """,
        (run_id,),
    )
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found.",
        )
    if run["ticket_customer_id"] != customer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied.",
        )
    return run


@router.get("/{run_id}", response_model=WorkflowRunResponse)
def get_run(
    run_id: str,
    customer_id: str = Depends(require_session),
) -> WorkflowRunResponse:
    run = _get_run_with_ownership(run_id, customer_id)
    return WorkflowRunResponse(
        run_id=run["id"],
        ticket_id=run["ticket_id"],
        thread_id=run["thread_id"],
        status=run["status"],
        started_at=run["started_at"],
        completed_at=run["completed_at"],
        final_response=run["final_response"],
    )


@router.get("/{run_id}/events", response_model=list[WorkflowEvent])
def get_run_events(
    run_id: str,
    customer_id: str = Depends(require_session),
) -> list[WorkflowEvent]:
    run = _get_run_with_ownership(run_id, customer_id)

    events = fetch_all(
        """
        SELECT id, run_id, actor, event_type, created_at
        FROM audit_events
        WHERE run_id = ?
           OR (
                run_id IS NULL
                AND event_type = 'ticket_created'
                AND payload LIKE ?
           )
        ORDER BY created_at ASC
        """,
        (run_id, f'%"ticket_id":"{run["ticket_id"]}"%'),
    )
    return [
        WorkflowEvent(
            event_id=e["id"],
            run_id=e["run_id"] or run_id,
            actor=e["actor"],
            event_type=e["event_type"],
            created_at=e["created_at"],
        )
        for e in events
    ]

class CustomerMessage(BaseModel):
    message: str


@router.post("/{run_id}/messages")
def submit_message(run_id: str, body: CustomerMessage, customer_id: str = Depends(require_session)):
    run = fetch_one(
        "SELECT r.id, r.status, r.thread_id, t.id AS ticket_id, "
        "t.customer_id, t.category "
        "FROM workflow_runs r JOIN tickets t ON t.id = r.ticket_id "
        "WHERE r.id = ?",
        (run_id,),
    )

    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if run["customer_id"] != customer_id:
        raise HTTPException(status_code=404, detail="Run not found.")
    if run["category"] != "order_placement":
        raise HTTPException(status_code=409, detail="This endpoint only accepts messages for order_placement runs.")
    if run["status"] != "paused":
        raise HTTPException(status_code=409, detail=f"Run is not awaiting a message (status={run['status']}).")

    decision = inspect_request(body.message, customer_id)
    if decision.action == "BLOCK":
        raise HTTPException(status_code=400, detail=f"Message blocked: {decision.reason}")

    config = {"configurable": {"thread_id": run_id}}
    try:
        result = order_placement_graph.invoke(Command(resume=decision.text), config=config)
    except Exception as e:
        execute("UPDATE workflow_runs SET status = 'failed' WHERE id = ?", (run_id,))
        log_event(run_id=run_id, actor="system", event_type="workflow_error",
                  payload=str(e))
        raise HTTPException(status_code=500, detail="Workflow failed to resume.")

    final_status = "failed" if result.get("error_code") else "completed"
    execute(
        "UPDATE workflow_runs SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (final_status, run_id),
    )

    return {
        "run_id": run_id,
        "status": final_status,
        "final_response": result.get("final_response"),
    }