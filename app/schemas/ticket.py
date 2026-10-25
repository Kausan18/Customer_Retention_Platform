# app/schemas/ticket.py
from typing import Literal
from pydantic import BaseModel, field_validator


VALID_CATEGORIES = Literal[
    "order_placement", "order_tracking", "order_status", "refund", "exchange"
]


class TicketCreate(BaseModel):
    order_id: str | None = None
    category: VALID_CATEGORIES
    message: str

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message must not be empty")
        if len(v) > 500:
            raise ValueError("message must not exceed 500 characters")
        return v


class TicketResponse(BaseModel):
    ticket_id: str
    customer_id: str
    order_id: str | None
    category: str
    status: str
    created_at: str