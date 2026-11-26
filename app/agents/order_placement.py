import json
import re
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from langgraph.checkpoint.memory import MemorySaver

from app.tools.catalog_tools import extract_catalog_keywords, search_catalog
from app.tools.ticket_tools import update_ticket
from app.db.database import execute as db_execute
from app.services.audit import log_event
from app.services.gemini_client import call_gemini
from app.schemas.workflow_state import CatalogSearchOutput, OrderConfirmationOutput

_MULTI_ADD_PHRASES = {
    "both", "all", "together", "all of them", "both of them",
    "order them together", "i would like to order them together",
    "i ll take both", "ill take both", "add all", "add both",
    "get both", "all three", "i want both", "i want all",
}

def _is_multi_add(text: str) -> bool:
    normalized = " ".join(re.sub(r"[\W_]+", " ", text.lower()).split())
    return any(phrase in normalized for phrase in _MULTI_ADD_PHRASES)


class PlacementState(TypedDict, total=False):
    run_id: str
    ticket_id: str
    customer_id: str
    customer_message: str
    catalog_results: Optional[list]
    offered_product_ids: Optional[list]
    customer_selection: Optional[str]
    validated_product: dict
    validated_products: Optional[list]
    cart_item: dict | None = None
    cart_items: list | None      
    needs_clarification: bool
    final_response: Optional[str]
    run_status: Optional[str]
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

    print(f"[DEBUG] search_catalog result: {result}")

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
friendly message. Use only product_id values from the list above, in the same
order as the products array. End your message by asking the customer to confirm which item they want by number or name before the order is placed. Do not place the order yet.

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
    db_execute(
        "UPDATE workflow_runs SET status = 'paused' WHERE id = ?",
        (state["run_id"],),
    )
    selection = interrupt({
        "offered_product_ids": state["offered_product_ids"],
        "message": state["final_response"],
    })
    return {**state, "customer_selection": selection}


def validate_selection_node(state: PlacementState) -> PlacementState:
    if state.get("error_code"):
        return state

    products_by_id = {
        product["product_id"]: product for product in state["catalog_results"]
    }
    offered_products = [
        products_by_id[pid] for pid in state["offered_product_ids"]
    ]
    offered_ids = state.get("offered_product_ids") or []
    customer_reply = (state.get("customer_selection") or "").strip()

    # Multi-add intent — customer wants all offered products
    if _is_multi_add(customer_reply):
        validated_products = [
            {
                "product_id": pid,
                "product_name": products_by_id[pid]["name"],
                "price_minor": products_by_id[pid]["price_minor"],
                "category": products_by_id[pid]["category"],
                "quantity": 1,
            }
            for pid in offered_ids
        ]
        return {
            **state,
            "validated_products": validated_products,
            "validated_product": validated_products[0],  # keep singular for compat
            "needs_clarification": False,
        }

    # Bare single confirmation — first offered product
    bare_confirmations = {
        "confirm", "yes", "ok", "okay", "sure", "1", "first",
        "that one", "this one", "go ahead",
    }
    normalized_reply = " ".join(re.sub(r"[\W_]+", " ", customer_reply.lower()).split())
    if normalized_reply in bare_confirmations and offered_ids:
        pid = offered_ids[0]
        return {
            **state,
            "validated_product": {
                "product_id": pid,
                "product_name": products_by_id[pid]["name"],
                "price_minor": products_by_id[pid]["price_minor"],
                "category": products_by_id[pid]["category"],
                "quantity": 1,
            },
            "validated_products": None,
            "needs_clarification": False,
        }

    # LLM maps reply to a single product
    prompt = f"""
[SYSTEM]
You are a customer support agent for TechMart.
Respond ONLY with valid JSON matching the schema below.

[CONTEXT]
Offered products (1-based numbering matches the order shown to the customer):
{json.dumps(offered_products, indent=2)}
Customer reply: {customer_reply}

[TASK]
Map the customer's reply to exactly one product_id from the list above.
- If they name a product, pick a number, or say yes/confirm/first/second → map to the correct product_id, needs_clarification false.
- If the intent is unclear → needs_clarification true, clarification_message explains the options.

[OUTPUT FORMAT]
{{
  "selected_product_id": "..." or null,
  "quantity": 1,
  "clarification_message": "...",
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

    if parsed.needs_clarification or parsed.selected_product_id not in offered_ids:
        return {
            **state,
            "customer_selection": None,
            "needs_clarification": True,
            "validated_products": None,
            "final_response": parsed.clarification_message.strip() or (
                "Please choose one of the listed products by number or name. "
                "I won't place the order until you confirm a specific item."
            ),
        }

    pid = parsed.selected_product_id
    return {
        **state,
        "validated_product": {
            "product_id": pid,
            "product_name": products_by_id[pid]["name"],
            "price_minor": products_by_id[pid]["price_minor"],
            "category": products_by_id[pid]["category"],
            "quantity": parsed.quantity,
        },
        "validated_products": None,
        "needs_clarification": False,
    }

def build_cart_response_node(state: PlacementState) -> PlacementState:
    # Multi-add path
    if state.get("validated_products"):
        items = [
            {**p, "quantity": p.get("quantity", 1)}
            for p in state["validated_products"]
        ]
        names = ", ".join(p["product_name"] for p in items)
        total = sum(p["price_minor"] * p["quantity"] for p in items)
        return {
            **state,
            "cart_items": items,
            "cart_item": None,
            "final_response": (
                f"Got it! I've added {names} to your cart "
                f"(₹{total / 100:.2f} total). "
                "You can add more items or type 'checkout' to proceed to payment."
            ),
            "run_status": "completed",
        }

    # Single-item path
    p = state["validated_product"]
    cart_item = {**p, "quantity": p.get("quantity", 1)}
    return {
        **state,
        "cart_item": cart_item,
        "cart_items": None,
        "final_response": (
            f"Got it! I've added {cart_item['product_name']} "
            f"(₹{cart_item['price_minor'] / 100:.2f}) to your cart. "
            "You can add more items or type 'checkout' to proceed to payment."
        ),
        "run_status": "completed",
    }

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
    db_execute(
        "UPDATE workflow_runs SET status = 'completed' WHERE id = ?",
        (state["run_id"],),
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
        db_execute(
            "UPDATE workflow_runs SET status = 'completed' WHERE id = ?",
            (state["run_id"],),
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
    db_execute(
        "UPDATE workflow_runs SET status = 'failed' WHERE id = ?",
        (state["run_id"],),
    )
    return {**state, "final_response": safe_message}

def route_after(state: PlacementState) -> str:
    return "handle_error_node" if state.get("error_code") else "continue"


def route_after_selection(state: PlacementState) -> str:
    if state.get("error_code"):
        return "handle_error_node"
    if state.get("needs_clarification"):
        return "ask_again"
    return "continue"


checkpointer = MemorySaver()

graph_builder = StateGraph(PlacementState)
graph_builder.add_node("classify_query_node", classify_query_node)
graph_builder.add_node("search_catalog_node", search_catalog_node)
graph_builder.add_node("present_options_node", present_options_node)
graph_builder.add_node("await_confirmation_node", await_confirmation_node)
graph_builder.add_node("validate_selection_node", validate_selection_node)
graph_builder.add_node("build_cart_response_node", build_cart_response_node)
graph_builder.add_node("update_ticket_node", update_ticket_node)
graph_builder.add_node("handle_error_node", handle_error_node)

graph_builder.add_edge(START, "classify_query_node")
graph_builder.add_edge("classify_query_node", "search_catalog_node")
graph_builder.add_conditional_edges("search_catalog_node", route_after,
    {"continue": "present_options_node", "handle_error_node": "handle_error_node"})
graph_builder.add_conditional_edges("present_options_node", route_after,
    {"continue": "await_confirmation_node", "handle_error_node": "handle_error_node"})
graph_builder.add_edge("await_confirmation_node", "validate_selection_node")
graph_builder.add_conditional_edges("validate_selection_node", route_after_selection,
    {
        "continue": "build_cart_response_node",
        "handle_error_node": "handle_error_node",
        "ask_again": "await_confirmation_node",
    })
graph_builder.add_edge("build_cart_response_node", "update_ticket_node")
graph_builder.add_edge("update_ticket_node", END)
graph_builder.add_edge("handle_error_node", END)

order_placement_graph = graph_builder.compile(checkpointer=checkpointer)