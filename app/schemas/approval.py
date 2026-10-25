# app/schemas/approval.py
from typing import Literal
from pydantic import BaseModel, field_validator


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    reviewer_id: str

    @field_validator("reviewer_id")
    @classmethod
    def reviewer_id_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("reviewer_id must not be empty")
        return v


class ApprovalResponse(BaseModel):
    approval_id: str
    proposal_id: str
    decision: str
    reviewer_id: str
    decided_at: str
    run_status: str