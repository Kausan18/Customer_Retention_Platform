# app/api/runs.py
"""
GET /runs/{run_id}         — poll workflow status
GET /runs/{run_id}/events  — audit event timeline (redacted)
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.session import require_session
from app.db.database import fetch_one, fetch_all
from app.schemas.workflow_state import WorkflowRunResponse, WorkflowEvent

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