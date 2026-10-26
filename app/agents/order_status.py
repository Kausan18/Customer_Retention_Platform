# app/agents/order_status.py
"""
order_status_graph — handles "what's my order status / is my payment
processed?" complaints. Structurally identical to order_tracking_graph;
differs only in which facts are fetched and the LLM prompt/schema.
"""

from typing import TypedDict, Optional
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from app.tools.order_tools import get_order, get_payment_status, get_recent_orders
from app.tools.ticket_tools import update_ticket
from app.services.audit import log_event
from app.guardrails.output_validator import validate_output
from app.services.gemini_client import call_gemini


class StatusSummaryOutput(BaseModel):
    summary: str


class OrderStatusState(TypedDict):
    customer_id: str
    ticket_id: str
    run_id: str
    customer_message: str
    order_id: Optional[str]
    order: Optional[dict]
    payment: Optional[dict]
    response: Optional[str]
    error: Optional[str]
    final_status: Optional[str]


def resolve_order_id_node(state: OrderStatusState) -> dict:
    stated = state.get("order_id")
    if stated:
        return {"order_id": stated}

    result = get_recent_orders(state["customer_id"], limit=1)
    if not result["success"]:
        return {"error": result["error_code"]}

    return {"order_id": result["orders"][0]["id"]}


def fetch_facts(state: OrderStatusState) -> dict:
    if state.get("error"):
        return {}

    order_result = get_order(state["order_id"], state["customer_id"])
    if not order_result["success"]:
        return {"error": order_result["error_code"]}

    payment_result = get_payment_status(state["order_id"])
    if not payment_result["success"]:
        return {"error": payment_result["error_code"]}

    return {"order": order_result["order"], "payment": payment_result["payment"]}


def summarize_status(state: OrderStatusState) -> dict:
    prompt = f"""
[SYSTEM]
You are a customer support agent for TechMart, an online electronics retailer.
You have access to verified order and payment data provided below.
You must not invent facts. Use only the data given.
Respond ONLY with valid JSON matching the schema. No preamble, no markdown.

[CONTEXT]
Order: {state["order"]}
Payment: {state["payment"]}

[TASK]
Summarize the current order and payment status in plain, friendly language.
State clearly whether payment is paid, pending, refunded, or partially refunded.
Do not claim a refund happened unless payment status explicitly shows it.

[OUTPUT FORMAT]
{{"summary": "string"}}
""".strip()

    raw = call_gemini(prompt)
    validated, err = validate_output(raw, StatusSummaryOutput)

    if err:
        retry_prompt = prompt + f"\n\n[PREVIOUS ERROR]\n{err}\nFix the output to match the schema exactly."
        raw_retry = call_gemini(retry_prompt)
        validated, err = validate_output(raw_retry, StatusSummaryOutput)

    if err:
        return {"error": "OUTPUT_VALIDATION_FAILED"}

    return {"response": validated.summary}


def update_ticket_node(state: OrderStatusState) -> dict:
    result = update_ticket(
        state["ticket_id"], "resolved", state["response"], state["customer_id"]
    )
    log_event(state["run_id"], state["customer_id"], "workflow_completed",
               {"ticket_id": state["ticket_id"], "category": "order_status"})

    if not result["success"]:
        return {"error": result["error_code"], "final_status": "escalated"}

    return {"final_status": "resolved"}


def handle_error(state: OrderStatusState) -> dict:
    safe_response = "We ran into an issue retrieving your order details. Your case has been escalated for review."
    update_ticket(state["ticket_id"], "escalated", safe_response, state["customer_id"])
    log_event(state["run_id"], state["customer_id"], "workflow_error",
               {"ticket_id": state["ticket_id"], "error_code": state.get("error")})
    return {"final_status": "escalated", "response": safe_response}


def route_after_resolve(state: OrderStatusState) -> str:
    return "handle_error" if state.get("error") else "fetch_facts"


def route_after_fetch(state: OrderStatusState) -> str:
    return "handle_error" if state.get("error") else "summarize_status"


def route_after_summarize(state: OrderStatusState) -> str:
    return "handle_error" if state.get("error") else "update_ticket_node"


builder = StateGraph(OrderStatusState)
builder.add_node("resolve_order_id_node", resolve_order_id_node)
builder.add_node("fetch_facts", fetch_facts)
builder.add_node("summarize_status", summarize_status)
builder.add_node("update_ticket_node", update_ticket_node)
builder.add_node("handle_error", handle_error)

builder.add_edge(START, "resolve_order_id_node")
builder.add_conditional_edges("resolve_order_id_node", route_after_resolve)
builder.add_conditional_edges("fetch_facts", route_after_fetch)
builder.add_conditional_edges("summarize_status", route_after_summarize)
builder.add_edge("update_ticket_node", END)
builder.add_edge("handle_error", END)

order_status_graph = builder.compile(checkpointer=MemorySaver())