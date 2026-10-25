# app/schemas/proposal.py
from pydantic import BaseModel


class ProposalResponse(BaseModel):
    proposal_id: str
    run_id: str
    action_type: str
    payload: dict
    status: str
    created_at: str
    expires_at: str