# app/schemas/approval.py
from typing import Literal
from pydantic import BaseModel


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]


class ApprovalResponse(BaseModel):
    approval_id: str
    proposal_id: str
    decision: str
    reviewer_id: str
    decided_at: str
    run_status: str