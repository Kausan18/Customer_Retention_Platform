# app/schemas/workflow_state.py
from typing import Literal
from pydantic import BaseModel


class WorkflowRunResponse(BaseModel):
    run_id: str
    ticket_id: str
    thread_id: str
    status: Literal["running", "paused", "completed", "failed"]
    started_at: str
    completed_at: str | None = None


class WorkflowEvent(BaseModel):
    event_id: str
    run_id: str
    actor: str
    event_type: str
    created_at: str