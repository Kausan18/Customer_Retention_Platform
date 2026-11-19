import json
import re
import uuid
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver

from mock_services import payment_service as mock_payment_service
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
    needs_clarification: bool
    payment_authorised: bool
    payment_result: dict
    payment_confirmation: Optional[str]
    payment_confirmed: bool
    payment_confirmation_invalid: bool
    order_result: dict
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
        products_by_id[product_id]
        for product_id in state["offered_product_ids"]
    ]

    prompt = f"""
[SYSTEM]
You are a customer support agent for TechMart.
Respond ONLY with valid JSON matching the schema below.

[CONTEXT]
Offered products in the same order shown to the customer
(option numbers are 1-based positions in this list):
{json.dumps(offered_products, indent=2)}
Customer reply: {state["customer_selection"]}

[TASK]
The customer was shown a list of products and asked to confirm their selection.
Look at their reply carefully.
- If they name a product, pick a number, or say yes/confirm/ok/that one/first/second etc → map to the correct product_id and set needs_clarification to false.
- If they ask a question, say "only one?", ask about more options, or do NOT make a clear selection → set needs_clarification to true and set clarification_message to a helpful reply explaining what options are available and asking them to pick one.
Never guess if the intent is unclear. When in doubt, set needs_clarification to true.

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

    if parsed.needs_clarification or parsed.selected_product_id not in state["offered_product_ids"]:
        clarification_message = parsed.clarification_message.strip()
        return {
            **state,
            "customer_selection": None,
            "needs_clarification": True,
            "final_response": clarification_message or (
                "Please choose one of the listed products by number or name. "
                "I won't place the order until you confirm a specific item."
            ),
        }

    return {
        **state,
        "selected_product_id": parsed.selected_product_id,
        "quantity": parsed.quantity,
        "needs_clarification": False,
    }


def show_payment_summary_node(state: PlacementState) -> PlacementState:
    selected_product = next(
        (
            product
            for product in state["catalog_results"]
            if product["product_id"] == state["selected_product_id"]
        ),
        None,
    )
    if selected_product is None:
        return {
            **state,
            "error_code": "PRODUCT_NOT_FOUND",
            "error_message": "The selected product is no longer available.",
        }

    quantity = state["quantity"]
    total_minor = selected_product["price_minor"] * quantity
    retry_message = (
        "Please reply 'confirm' to place this order or 'cancel' to stop.\n\n"
        if state.get("payment_confirmation_invalid")
        else ""
    )
    summary = (
        f"{retry_message}Great choice! Here's your order summary:\n\n"
        f"- Product: {selected_product['name']} x {quantity}\n"
        f"- Price: ₹{total_minor / 100:.2f}\n"
        "- Payment: Mock Card •••• 4242\n\n"
        "Type 'confirm' to place your order, or 'cancel' to go back."
    )
    confirmation = interrupt({"message": summary, "stage": "payment_confirmation"})
    return {
        **state,
        "final_response": summary,
        "payment_confirmation": confirmation,
        "payment_confirmation_invalid": False,
    }


def validate_payment_confirmation_node(state: PlacementState) -> PlacementState:
    if state.get("error_code"):
        return state

    reply = (state.get("payment_confirmation") or "").lower()
    if re.search(r"\b(cancel|no)\b", reply):
        return {
            **state,
            "payment_confirmed": False,
            "payment_confirmation_invalid": False,
            "error_code": "ORDER_CANCELLED",
            "error_message": "Order cancelled. Let me know if you'd like to try again.",
        }
    if re.search(r"\b(confirm|yes|ok(?:ay)?)\b", reply):
        return {
            **state,
            "payment_confirmed": True,
            "payment_confirmation_invalid": False,
        }
    return {
        **state,
        "payment_confirmed": False,
        "payment_confirmation_invalid": True,
    }


def process_payment_node(state: PlacementState) -> PlacementState:
    if state.get("error_code"):
        return state
    if not state.get("payment_confirmed"):
        return {
            **state,
            "payment_authorised": False,
            "payment_result": {
                "success": False,
                "error_code": "PAYMENT_NOT_CONFIRMED",
            },
            "error_code": "PAYMENT_FAILED",
            "error_message": "Payment requires customer confirmation.",
        }

    selected_product = next(
        (
            product
            for product in state["catalog_results"]
            if product["product_id"] == state["selected_product_id"]
        ),
        None,
    )
    if selected_product is None:
        return {
            **state,
            "payment_authorised": False,
            "payment_result": {
                "success": False,
                "error_code": "PRODUCT_NOT_FOUND",
            },
            "error_code": "PAYMENT_FAILED",
            "error_message": "The selected product is no longer available.",
        }

    quantity = state.get("quantity")
    if not isinstance(quantity, int) or quantity < 1:
        return {
            **state,
            "payment_authorised": False,
            "payment_result": {
                "success": False,
                "error_code": "INVALID_QUANTITY",
            },
            "error_code": "PAYMENT_FAILED",
            "error_message": "The selected quantity is invalid.",
        }

    amount_minor = selected_product["price_minor"] * quantity
    payment_result = mock_payment_service.charge(
        order_id=None,
        amount_minor=amount_minor,
        payment_method="mock_card",
    )
    if not payment_result.get("success"):
        return {
            **state,
            "payment_authorised": False,
            "payment_result": payment_result,
            "error_code": "PAYMENT_FAILED",
            "error_message": payment_result.get(
                "message",
                "The payment could not be authorized.",
            ),
        }

    return {
        **state,
        "payment_authorised": True,
        "payment_result": payment_result,
    }


def create_order_node(state: PlacementState) -> PlacementState:
    if state.get("error_code"):
        return state

    if not state.get("payment_authorised"):
        return {
            **state,
            "error_code": "PAYMENT_FAILED",
            "error_message": "Payment must be authorized before creating an order.",
        }

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

    return {
        **state,
        "new_order_id": result["order_id"],
        "order_result": result,
    }


def final_response_node(state: PlacementState) -> PlacementState:
    if state.get("error_code"):
        return state

    prompt = f"""
[SYSTEM]
You are a customer support agent for TechMart. Confirm the order in a
short, friendly message. Only use the facts provided below — do not
invent shipping dates or carrier names.

[CONTEXT]
Order result:
{json.dumps(state["order_result"], indent=2)}
Payment result:
{json.dumps(state["payment_result"], indent=2)}

[TASK]
Write a friendly order confirmation message for the customer. You MUST include all of the following on separate lines:
- Order ID
- Product name and quantity
- Total amount paid in INR (convert from paise: divide by 100)
- Payment method (from payment_result)
- Order placed date (today's date from tool results)
- Estimated delivery: 5-7 business days from today
- A note that they can ask "where is my order?" anytime to track it

Use only facts from the tool results. Do not invent any details.

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
    if state.get("error_code") == "ORDER_CANCELLED":
        safe_message = state.get("error_message") or (
            "Order cancelled. Let me know if you'd like to try again."
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
            payload=json.dumps({"outcome": "order_cancelled"}),
        )
        return {
            **state,
            "final_response": safe_message,
            "error_code": None,
            "error_message": None,
        }

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
        "PAYMENT_FAILED": "I couldn't authorize the payment, so no order was placed. Please try again.",
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


def route_after_selection(state: PlacementState) -> str:
    if state.get("error_code"):
        return "handle_error_node"
    if state.get("needs_clarification"):
        return "ask_again"
    return "continue"


def route_after_payment_confirmation(state: PlacementState) -> str:
    if state.get("error_code"):
        return "handle_error_node"
    if state.get("payment_confirmation_invalid"):
        return "ask_again"
    if state.get("payment_confirmed"):
        return "continue"
    return "handle_error_node"


checkpointer = MemorySaver()

graph_builder = StateGraph(PlacementState)
graph_builder.add_node("classify_query_node", classify_query_node)
graph_builder.add_node("search_catalog_node", search_catalog_node)
graph_builder.add_node("present_options_node", present_options_node)
graph_builder.add_node("await_confirmation_node", await_confirmation_node)
graph_builder.add_node("validate_selection_node", validate_selection_node)
graph_builder.add_node("show_payment_summary_node", show_payment_summary_node)
graph_builder.add_node("validate_payment_confirmation_node", validate_payment_confirmation_node)
graph_builder.add_node("process_payment_node", process_payment_node)
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
graph_builder.add_conditional_edges("validate_selection_node", route_after_selection,
    {
        "continue": "show_payment_summary_node",
        "handle_error_node": "handle_error_node",
        "ask_again": "await_confirmation_node",
    })
graph_builder.add_edge("show_payment_summary_node", "validate_payment_confirmation_node")
graph_builder.add_conditional_edges("validate_payment_confirmation_node", route_after_payment_confirmation,
    {
        "continue": "process_payment_node",
        "ask_again": "show_payment_summary_node",
        "handle_error_node": "handle_error_node",
    })
graph_builder.add_conditional_edges("process_payment_node", route_after,
    {"continue": "create_order_node", "handle_error_node": "handle_error_node"})
graph_builder.add_conditional_edges("create_order_node", route_after,
    {"continue": "final_response_node", "handle_error_node": "handle_error_node"})
graph_builder.add_edge("final_response_node", "update_ticket_node")
graph_builder.add_edge("update_ticket_node", END)
graph_builder.add_edge("handle_error_node", END)

order_placement_graph = graph_builder.compile(checkpointer=checkpointer)