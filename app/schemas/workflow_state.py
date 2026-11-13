# app/schemas/workflow_state.py
from typing import Literal, Optional
from pydantic import BaseModel,Field, field_validator


class WorkflowRunResponse(BaseModel):
    run_id: str
    ticket_id: str
    thread_id: str
    status: Literal["running", "paused", "completed", "failed"]
    started_at: str
    completed_at: str | None = None
    final_response: Optional[str] = None


class WorkflowEvent(BaseModel):
    event_id: str
    run_id: str
    actor: str
    event_type: str
    created_at: str



class CatalogOption(BaseModel):
    product_id: str
    name: str
    category: str
    price_minor: int
    price_display: str


class CatalogSearchOutput(BaseModel):
    """
    Output schema for present_options_node. The LLM must only reference
    product_ids returned by search_catalog(); that check runs in code.
    """
    products: list[CatalogOption] = Field(default_factory=list)
    message: str

    @field_validator("products")
    @classmethod
    def max_options(cls, v):
        if len(v) > 5:
            raise ValueError("Cannot present more than 5 options")
        return v


class OrderConfirmationOutput(BaseModel):
    """
    Output schema for validate_selection_node. selected_product_id is
    cross-checked against offered_product_ids in code before place_order().
    """
    selected_product_id: Optional[str] = None
    quantity: int = 1
    confirmation_message: str
    needs_clarification: bool = False

    @field_validator("quantity")
    @classmethod
    def positive_quantity(cls, v):
        if v < 1:
            raise ValueError("quantity must be at least 1")
        return v

class OrderPlacementState(BaseModel):
    run_id: str
    ticket_id: str
    customer_id: str
    customer_message: str
    catalog_results: Optional[list] = None
    offered_product_ids: Optional[list[str]] = None
    customer_selection: Optional[str] = None
    selected_product_id: Optional[str] = None
    quantity: Optional[int] = None
    new_order_id: Optional[str] = None
    final_response: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None