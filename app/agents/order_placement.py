import json
import uuid
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver

from app.tools.catalog_tools import extract_catalog_keywords, search_catalog, place_order
from app.tools.ticket_tools import update_ticket
from app.services.audit import log_event
from app.services.gemini_client import call_gemini
from app.schemas.workflow_state import CatalogSearchOutput, OrderConfirmationOutput


class PlacementState(TypedDict, total=False):
    run_id: str
    ticket_id: str
    customer_id: str
    customer_message: str
    catalog_results: Optional[list]
    offered_product_ids: Optional[list]
    customer_selection: Optional[str]
    selected_product_id: Optional[str]
    quantity: Optional[int]
    new_order_id: Optional[str]
    final_response: Optional[str]
    error_code: Optional[str]
    error_message: Optional[str]


def classify_query_node(state: PlacementState) -> PlacementState:
    # V1 scope: always treat the message as a natural-language catalog
    # query. No separate "named product" branch yet — search_catalog()
    # handles both via simple keyword matching.
    return state


def search_catalog_node(state: PlacementState) -> PlacementState:
    keywords = extract_catalog_keywords(state["customer_message"])
    query = " ".join(keywords)
    result = search_catalog(query=query) if query else {"success": True, "products": []}
    if not result.get("success") or not result.get("products"):
        fallback_query = max(keywords, key=len, default="")
        if fallback_query:
            result = search_catalog(query=fallback_query)

    if not result.get("success") or not result.get("products"):
        return {
            **state,
            "error_code": "NO_CATALOG_RESULTS",
            "error_message": (
                "The requested product was not found in the catalog. "
                "Please browse the available categories and let us know what you would like."
            ),
        }
    return {**state, "catalog_results": result["products"]}


def present_options_node(state: PlacementState) -> PlacementState:
    if state.get("error_code"):
        return state

    products = state["catalog_results"][:5]
    offered_ids = [p["product_id"] for p in products]

    prompt = f"""
[SYSTEM]
You are a customer support agent for TechMart, an online electronics retailer.
You must respond ONLY with valid JSON matching the schema below. No preamble,
no markdown, no explanation outside the JSON.

[CONTEXT]
Customer message: {state["customer_message"]}
Available products (do not invent any product not in this list):
{json.dumps(products, indent=2)}

[TASK]
Present up to 3 of the most relevant products to the customer in a short,
friendly message. Use only product_id values from the list above.

[OUTPUT FORMAT]
{{
  "products": [
    {{"product_id": "...", "name": "...", "category": "...",
      "price_minor": 0, "price_display": "₹..."}}
  ],
  "message": "..."
}}
"""
    raw = call_gemini(prompt)

    try:
        cleaned = raw.strip().removeprefix("```json").removesuffix("```").strip()
        parsed = CatalogSearchOutput.model_validate_json(cleaned)
    except Exception as e:
        return {
            **state,
            "error_code": "OUTPUT_VALIDATION_FAILED",
            "error_message": f"present_options_node validation failed: {e}",
        }

    # Enforce: every product_id the LLM returned must be in the offered set
    valid_ids = {p["product_id"] for p in products}
    for p in parsed.products:
        if p.product_id not in valid_ids:
            return {
                **state,
                "error_code": "INVALID_PRODUCT_REFERENCE",
                "error_message": f"LLM referenced unknown product_id {p.product_id}",
            }

    return {
        **state,
        "offered_product_ids": [p.product_id for p in parsed.products],
        "final_response": parsed.message,
    }


def await_confirmation_node(state: PlacementState) -> PlacementState:
    if state.get("error_code"):
        return state
    selection = interrupt({
        "offered_product_ids": state["offered_product_ids"],
        "message": state["final_response"],
    })
    return {**state, "customer_selection": selection}


def validate_selection_node(state: PlacementState) -> PlacementState:
    if state.get("error_code"):
        return state

    prompt = f"""
[SYSTEM]
You are a customer support agent for TechMart.
Respond ONLY with valid JSON matching the schema below.

[CONTEXT]
Offered product IDs: {state["offered_product_ids"]}
Customer reply: {state["customer_selection"]}

[TASK]
Determine which product_id the customer selected and the quantity
(default 1 if not stated). If the reply does not clearly map to one of
the offered product_ids, set needs_clarification to true and leave
selected_product_id null.

[OUTPUT FORMAT]
{{
  "selected_product_id": "..." or null,
  "quantity": 0,
  "confirmation_message": "...",
  "needs_clarification": false
}}
"""
    raw = call_gemini(prompt)

    try:
        cleaned = raw.strip().removeprefix("```json").removesuffix("```").strip()
        parsed = OrderConfirmationOutput.model_validate_json(cleaned)
    except Exception as e:
        return {
            **state,
            "error_code": "OUTPUT_VALIDATION_FAILED",
            "error_message": f"validate_selection_node validation failed: {e}",
        }

    if parsed.needs_clarification or parsed.selected_product_id not in state["offered_product_ids"]:
        return {
            **state,
            "error_code": "SELECTION_UNCLEAR",
            "error_message": "Could not match the customer's reply to an offered product.",
        }

    return {
        **state,
        "selected_product_id": parsed.selected_product_id,
        "quantity": parsed.quantity,
    }


def create_order_node(state: PlacementState) -> PlacementState:
    if state.get("error_code"):
        return state

    result = place_order(
        customer_id=state["customer_id"],
        items=[{"product_id": state["selected_product_id"], "quantity": state["quantity"]}],
    )

    if not result.get("success"):
        return {
            **state,
            "error_code": result.get("error_code", "ORDER_CREATION_FAILED"),
            "error_message": result.get("message", "Order creation failed."),
        }

    log_event(
        run_id=state["run_id"],
        actor="system",
        event_type="order_created",
        payload=json.dumps({"order_id": result["order_id"], "total_minor": result["total_minor"]}),
    )

    return {**state, "new_order_id": result["order_id"]}


def final_response_node(state: PlacementState) -> PlacementState:
    if state.get("error_code"):
        return state

    prompt = f"""
[SYSTEM]
You are a customer support agent for TechMart. Confirm the order in a
short, friendly message. Only use the facts provided below — do not
invent shipping dates or carrier names.

[CONTEXT]
Order ID: {state["new_order_id"]}
Quantity: {state["quantity"]}

[TASK]
Write a 1-2 sentence order confirmation referencing the order ID.

[OUTPUT FORMAT]
Plain text only, no JSON.
"""
    raw = call_gemini(prompt)
    return {**state, "final_response": raw.strip()}


def update_ticket_node(state: PlacementState) -> PlacementState:
    update_ticket(
        ticket_id=state["ticket_id"],
        status="resolved",
        note=state.get("final_response", ""),
        customer_id=state["customer_id"],
    )
    log_event(
        run_id=state["run_id"],
        actor="system",
        event_type="workflow_completed",
        payload=None,
    )
    return state


def handle_error_node(state: PlacementState) -> PlacementState:
    if state.get("error_code") == "NO_CATALOG_RESULTS":
        safe_message = (
            "I couldn't find that product in our catalog. You can browse the available "
            "categories and let me know what you'd like."
        )
        update_ticket(
            ticket_id=state["ticket_id"],
            status="resolved",
            note=safe_message,
            customer_id=state["customer_id"],
        )
        log_event(
            run_id=state["run_id"],
            actor="system",
            event_type="workflow_completed",
            payload=json.dumps({"outcome": "product_not_found"}),
        )
        return {
            **state,
            "final_response": safe_message,
            "error_code": None,
            "error_message": None,
        }

    safe_message = {
        "NO_PRODUCTS_FOUND": "I couldn't find any matching products in our catalog. A team member will follow up.",
        "SELECTION_UNCLEAR": "I couldn't tell which option you meant. Could you reply with the product name or number?",
    }.get(state.get("error_code"), "Something went wrong processing your order. This has been escalated.")

    update_ticket(
        ticket_id=state["ticket_id"],
        status="escalated",
        note=safe_message,
        customer_id=state["customer_id"],
    )
    log_event(
        run_id=state["run_id"],
        actor="system",
        event_type="workflow_error",
        payload=json.dumps({"error_code": state.get("error_code"), "error_message": state.get("error_message")}),
    )
    return {**state, "final_response": safe_message}


def route_after(state: PlacementState) -> str:
    return "handle_error_node" if state.get("error_code") else "continue"


checkpointer = MemorySaver()

graph_builder = StateGraph(PlacementState)
graph_builder.add_node("classify_query_node", classify_query_node)
graph_builder.add_node("search_catalog_node", search_catalog_node)
graph_builder.add_node("present_options_node", present_options_node)
graph_builder.add_node("await_confirmation_node", await_confirmation_node)
graph_builder.add_node("validate_selection_node", validate_selection_node)
graph_builder.add_node("create_order_node", create_order_node)
graph_builder.add_node("final_response_node", final_response_node)
graph_builder.add_node("update_ticket_node", update_ticket_node)
graph_builder.add_node("handle_error_node", handle_error_node)

graph_builder.add_edge(START, "classify_query_node")
graph_builder.add_edge("classify_query_node", "search_catalog_node")
graph_builder.add_conditional_edges("search_catalog_node", route_after,
    {"continue": "present_options_node", "handle_error_node": "handle_error_node"})
graph_builder.add_conditional_edges("present_options_node", route_after,
    {"continue": "await_confirmation_node", "handle_error_node": "handle_error_node"})
graph_builder.add_edge("await_confirmation_node", "validate_selection_node")
graph_builder.add_conditional_edges("validate_selection_node", route_after,
    {"continue": "create_order_node", "handle_error_node": "handle_error_node"})
graph_builder.add_conditional_edges("create_order_node", route_after,
    {"continue": "final_response_node", "handle_error_node": "handle_error_node"})
graph_builder.add_edge("final_response_node", "update_ticket_node")
graph_builder.add_edge("update_ticket_node", END)
graph_builder.add_edge("handle_error_node", END)

order_placement_graph = graph_builder.compile(checkpointer=checkpointer)