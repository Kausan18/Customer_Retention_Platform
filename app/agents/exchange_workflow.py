"""
Exchange workflow graph with LangGraph interrupt/resume.

Node flow:
  START → investigate → retrieve_policy → check_eligibility
        ↓ [ineligible]  explain_ineligible → update_ticket_node → END
        ↓ [eligible]    search_catalog_node → check_catalog_node
        ↓ [no product]  handle_error → END
        ↓ [same-category replacement] create_proposal → await_approval  ← INTERRUPT
        ↓ [no valid replacement] handle_error → END
                                                          ↓ RESUME
                                         revalidate
                                         ↓ [rejected]  handle_rejection → update_ticket_node → END
                                         ↓ [approved]  execute_exchange  → update_ticket_node → END
                                         ↓ [error]     handle_error → END

Key differences from refund_workflow:
- retrieve_policy queries exchange policy, not refund policy
- search_catalog_node auto-selects first in-stock replacement in same category
- execute_exchange creates a new order; no payment service call
"""
import json
import re
from typing import Optional, Any
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, END, START
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command  # noqa: F401

from pydantic import BaseModel

from app.tools.order_tools import get_order, get_payment_status
from app.tools.policy_tools import search_policy_tool
from app.tools.ticket_tools import update_ticket
from app.tools.catalog_tools import (
    extract_catalog_keywords,
    search_catalog,
    create_exchange_proposal,
    execute_approved_exchange,
)
from app.services.eligibility import check_exchange_eligibility
from app.services.gemini_client import call_gemini
from app.guardrails.output_validator import validate_output
from app.services.audit import log_event

VALID_CATEGORIES = {"Electronics", "Clothing", "Cosmetics", "Food", "Shoes", "Toys"}


def extract_requested_category(message: str) -> str | None:
    for cat in VALID_CATEGORIES:
        if re.search(rf"\b{cat}\b", message, re.IGNORECASE):
            return cat
    return None


# ─── LLM output schemas ───────────────────────────────────────────────────────

class ExchangeIneligibleOutput(BaseModel):
    explanation: str
    policy_ref: str


class ExchangeRejectionOutput(BaseModel):
    explanation: str


# ─── State ────────────────────────────────────────────────────────────────────

class ExchangeState(TypedDict):
    run_id: str
    ticket_id: str
    customer_id: str
    order_id: Optional[str]
    complaint_message: str
    order: Optional[dict]
    payment: Optional[dict]
    policy_passages: Optional[list]
    eligibility_result: Optional[dict]
    catalog_results: Optional[list]
    selected_replacement: Optional[dict]
    proposal_id: Optional[str]
    reviewer_decision: Optional[Any]
    execution_result: Optional[dict]
    error_code: Optional[str]
    error_message: Optional[str]
    final_response: Optional[str]


# ─── Nodes ────────────────────────────────────────────────────────────────────

def investigate_node(state: ExchangeState) -> dict:
    order_id = state.get("order_id")
    customer_id = state["customer_id"]

    if not order_id:
        return {
            "final_response": (
                "I couldn't find an order matching your request. "
                "Could you please provide your order ID so I can look into the exchange for you?"
            ),
            "error_code": "ORDER_NOT_FOUND",
        }

    order = get_order(order_id, customer_id)
    if not order.get("success") or not order_id:
        return {
            "final_response": (
                "I couldn't find an order matching your request. "
                "Could you please provide your order ID so I can look into the exchange for you?"
            ),
            "error_code": "ORDER_NOT_FOUND",
        }

    payment = get_payment_status(order_id)
    return {
        "order_id": order_id,
        "order": order,
        "payment": payment if payment.get("success") else None,
    }


def retrieve_policy_node(state: ExchangeState) -> dict:
    result = search_policy_tool("exchange eligibility window same category conditions")
    passages = result.get("passages", []) if result.get("success") else []
    return {"policy_passages": passages}


def check_eligibility_node(state: ExchangeState) -> dict:
    order_id = state.get("order_id")
    if not order_id:
        return {
            "eligibility_result": {
                "eligible": False,
                "error_code": "NO_ORDER_FOUND",
                "reason": "No order ID in state.",
            }
        }
    return {"eligibility_result": check_exchange_eligibility(order_id)}


def explain_ineligible_node(state: ExchangeState) -> dict:
    catalog_error_responses = {
        "ORDER_NOT_FOUND": (
            "I couldn't find an order matching your request. "
            "Could you please provide your order ID so I can look into the exchange for you?"
        ),
        "NO_SAME_CATEGORY_ITEM": (
            "We weren't able to find a replacement item in the same category as your original order. "
            "Per our exchange policy, exchanges must be for a product in the same category."
        ),
        "NO_CATALOG_RESULTS": (
            "We weren't able to find any available products matching your request. "
            "Please contact support for assistance finding a replacement."
        ),
    }
    catalog_error_response = catalog_error_responses.get(state.get("error_code"))
    if catalog_error_response:
        return {
            "final_response": catalog_error_response,
            "error_code": None,
            "error_message": None,
        }

    eligibility = state.get("eligibility_result", {})
    passages = state.get("policy_passages", [])
    policy_text = (
        "\n".join(f"[{p['document']}:{p['section']}] {p['content'][:300]}" for p in passages)
        if passages
        else "No policy passages retrieved."
    )

    prompt = f"""[SYSTEM]
You are a customer support agent for TechMart. Respond ONLY with valid JSON.
Do not invent facts. Use only the provided context.

[CONTEXT]
Customer ID: {state['customer_id']}
Order ID: {state.get('order_id')}
Eligibility Result: {json.dumps(eligibility)}
Policy Passages:
{policy_text}

[TASK]
Explain to the customer why their exchange request cannot be processed.
Reference the specific policy condition not met. Be empathetic, no workarounds.

[OUTPUT FORMAT]
{{
  "explanation": "string — 2-3 sentence customer-facing explanation",
  "policy_ref": "string — e.g. exchange_policy_v1:eligibility_conditions"
}}
"""
    raw = call_gemini(prompt)
    result, error = validate_output(raw, ExchangeIneligibleOutput)
    if error:
        raw2 = call_gemini(f"{prompt}\n\nValidation failed: {error}\nPlease fix and retry.")
        result, error2 = validate_output(raw2, ExchangeIneligibleOutput)
        if error2:
            return {
                "final_response": (
                    "We're unable to process your exchange request at this time. "
                    "Please review our exchange policy or contact support."
                ),
                "error_code": "OUTPUT_VALIDATION_FAILED",
            }
    explanation = (result.explanation or "").strip()
    if not explanation:
        reason = eligibility.get("reason")
        if eligibility.get("error_code") == "NO_ORDER_FOUND":
            reason = "We could not identify an order for your exchange request."
        reason = reason or "Your order does not meet the exchange eligibility conditions."
        explanation = (
            f"{reason} Per our exchange policy, exchanges require a delivered order "
            "within 15 days of delivery and a fully paid, unrefunded payment."
        )
    return {"final_response": explanation}


def search_catalog_node(state: ExchangeState) -> dict:
    """
    Auto-select a replacement product in the same category as the first order item.
    Excludes products the customer already owns in this order.
    """
    eligibility = state.get("eligibility_result", {})
    items = eligibility.get("items", [])

    if not items:
        return {
            "error_code": "NO_ITEMS_FOUND",
            "error_message": "Could not determine order items for exchange.",
        }

    # Use first item's category for same-category constraint
    category = items[0]["category"]
    exclude_ids = [item["product_id"] for item in items]
    message = state.get("complaint_message", "")
    requested_category = extract_requested_category(message)
    original_categories = {item.get("category") for item in items if item.get("category")}

    if requested_category:
        if requested_category not in original_categories:
            return {
                "error_code": "NO_SAME_CATEGORY_ITEM",
                "error_message": (
                    "The requested product is not in the same category as the original item. "
                    "The exchange policy requires a same-category replacement."
                ),
            }
        search_catalog(query=requested_category)
    else:
        keywords = extract_catalog_keywords(message)
        if keywords:
            requested_catalog = search_catalog(query=" ".join(keywords))
            if not requested_catalog.get("success") or not requested_catalog.get("products"):
                fallback_query = max(keywords, key=len, default="")
                if fallback_query:
                    requested_catalog = search_catalog(query=fallback_query)
            requested_categories = {
                product.get("category")
                for product in requested_catalog.get("products", [])
                if product.get("category")
            }
        else:
            requested_categories = set()
        if requested_categories and not requested_categories.intersection(original_categories):
            return {
                "error_code": "NO_SAME_CATEGORY_ITEM",
                "error_message": (
                    "The requested product is not in the same category as the original item. "
                    "The exchange policy requires a same-category replacement."
                ),
            }

    catalog = search_catalog(category=category, exclude_product_ids=exclude_ids)

    if not catalog.get("success") or not catalog.get("products"):
        return {
            "error_code": "NO_CATALOG_RESULTS",
            "error_message": (
                f"No replacement products are available in the {category} category. "
                "Please browse the available categories and let us know what you would like."
            ),
        }

    products = catalog["products"]
    # Auto-select: first available product
    selected = products[0]

    return {"catalog_results": products, "selected_replacement": selected}


def check_catalog_node(state: ExchangeState) -> dict:
    """Reject exchanges whose explicitly requested replacement is another category."""
    eligibility = state.get("eligibility_result") or {}
    items = eligibility.get("items") or []
    original_categories = {item.get("category") for item in items if item.get("category")}
    catalog_results = state.get("catalog_results") or []

    if state.get("error_code"):
        return {}

    if not catalog_results or not any(
        product.get("category") in original_categories
        for product in catalog_results
    ):
        return {
            "error_code": "NO_CATALOG_RESULTS",
            "error_message": (
                "The exchange policy requires a replacement from the same category "
                "as the original item, but no matching replacement is available. "
                "Please browse the available categories and let us know what you would like."
            ),
        }

    requested_category = extract_requested_category(state.get("complaint_message", ""))
    if requested_category:
        if requested_category not in original_categories:
            return {
                "error_code": "NO_SAME_CATEGORY_ITEM",
                "error_message": (
                    "The requested replacement is not in the same category as the "
                    "original item. The exchange policy requires a same-category replacement."
                ),
            }
        return {}

    requested_item = re.search(
        r"\bfor\s+(?:(?:a|an|the)\s+)?([^.!?,]+)",
        state.get("complaint_message", ""),
        flags=re.IGNORECASE,
    )
    requested_query = requested_item.group(1).strip() if requested_item else ""
    if requested_query and not re.search(
        r"\b(?:different|another|same|similar|any|replacement|item|product)\b",
        requested_query,
        flags=re.IGNORECASE,
    ):
        requested_catalog = search_catalog(query=requested_query)
        requested_categories = {
            product.get("category")
            for product in requested_catalog.get("products", [])
            if product.get("category")
        }
        if not requested_categories:
            return {
                "error_code": "NO_CATALOG_RESULTS",
                "error_message": (
                    f"No catalog replacement matching '{requested_query}' is available. "
                    "Please browse the available categories and let us know what you would like."
                ),
            }
        if not requested_categories.intersection(original_categories):
            return {
                "error_code": "NO_SAME_CATEGORY_ITEM",
                "error_message": (
                    "The requested replacement is not in the same category as the "
                    "original item. The exchange policy requires a same-category replacement."
                ),
            }

    return {}


def create_proposal_node(state: ExchangeState) -> dict:
    """Code node: create exchange proposal in DB. Idempotent."""
    eligibility = state["eligibility_result"]
    replacement = state["selected_replacement"]
    passages = state.get("policy_passages") or []

    policy_refs = [f"{p['document']}:{p['section']}" for p in passages[:2]]

    proposal = create_exchange_proposal(
        run_id=state["run_id"],
        original_order_id=state["order_id"],
        original_items=eligibility.get("items", []),
        replacement_product_id=replacement["product_id"],
        replacement_product_name=replacement["name"],
        replacement_price_minor=replacement["price_minor"],
        reason=eligibility.get("reason", "Exchange requested by customer"),
        policy_refs=policy_refs,
    )

    if not proposal.get("success"):
        return {
            "error_code": "PROPOSAL_CREATION_FAILED",
            "error_message": "Failed to create exchange proposal.",
        }
    return {"proposal_id": proposal["proposal_id"]}


def await_approval_node(state: ExchangeState) -> dict:
    decision = interrupt(
        {
            "proposal_id": state["proposal_id"],
            "run_id": state["run_id"],
            "pending_action": "awaiting_approval",
        }
    )
    return {"reviewer_decision": decision}


def revalidate_node(state: ExchangeState) -> dict:
    reviewer_decision = state.get("reviewer_decision")
    if isinstance(reviewer_decision, dict):
        decision = reviewer_decision.get("decision", "rejected")
    else:
        decision = str(reviewer_decision) if reviewer_decision else "rejected"

    if decision == "rejected":
        return {"error_code": "PROPOSAL_REJECTED", "reviewer_decision": decision}

    re_check = check_exchange_eligibility(state["order_id"])
    if not re_check.get("eligible"):
        return {
            "error_code": "ELIGIBILITY_CHANGED_AFTER_APPROVAL",
            "error_message": f"Order no longer exchange-eligible: {re_check.get('error_code')}",
            "reviewer_decision": decision,
        }
    return {"reviewer_decision": decision}


def execute_exchange_node(state: ExchangeState) -> dict:
    result = execute_approved_exchange(state["proposal_id"], state["customer_id"])

    if not result.get("success"):
        return {
            "error_code": result.get("error_code", "EXCHANGE_EXECUTION_FAILED"),
            "error_message": result.get("message", "Exchange execution failed."),
            "execution_result": result,
        }

    return {
        "execution_result": result,
        "final_response": (
            f"Your exchange has been approved! A replacement "
            f"{result['replacement_product']} is being prepared and will be shipped "
            f"within 3–5 business days. Your new order ID is {result['new_order_id']}."
        ),
    }


def handle_rejection_node(state: ExchangeState) -> dict:
    prompt = f"""[SYSTEM]
You are a customer support agent for TechMart. Respond ONLY with valid JSON.

[CONTEXT]
Customer ID: {state['customer_id']}
Order ID: {state.get('order_id')}

[TASK]
Inform the customer that their exchange request was reviewed by our support team
and was not approved at this time. Be professional and empathetic.

[OUTPUT FORMAT]
{{"explanation": "string — 2-3 sentence customer-facing message"}}
"""
    raw = call_gemini(prompt)
    result, error = validate_output(raw, ExchangeRejectionOutput)
    if error:
        return {
            "final_response": (
                "Your exchange request has been reviewed by our support team. "
                "At this time, we are unable to approve the exchange. "
                "Please contact support for further assistance."
            )
        }
    return {"final_response": result.explanation}


def update_ticket_node(state: ExchangeState) -> dict:
    final_response = state.get("final_response") or "Exchange workflow completed."
    error_code = state.get("error_code")

    if error_code == "PROPOSAL_REJECTED":
        status = "resolved"
    elif error_code:
        status = "escalated"
        final_response = state.get("error_message") or final_response
    else:
        status = "resolved"

    update_ticket(state["ticket_id"], status, final_response, state["customer_id"])
    log_event(
        state["run_id"],
        "system",
        "workflow_completed",
        json.dumps({"final_status": status, "error_code": error_code}),
    )
    return {}


def handle_error_node(state: ExchangeState) -> dict:
    error_code = state.get("error_code", "UNKNOWN_ERROR")
    message = state.get("error_message") or "Please contact support for assistance."
    if error_code == "ORDER_NOT_FOUND" or not state.get("order_id"):
        state["final_response"] = (
            "I couldn't find an order matching your request. "
            "Could you provide your order ID so I can look into this for you?"
        )
    else:
        state["final_response"] = (
            "I wasn't able to process your exchange request. " + message
        )
    update_ticket(
        state["ticket_id"],
        "escalated",
        state["final_response"],
        state["customer_id"],
    )
    log_event(state["run_id"], "system", "workflow_error",
              json.dumps({"error_code": error_code, "message": message}))
    return {"final_response": state["final_response"]}


# ─── Routing functions ─────────────────────────────────────────────────────────

def _route_after_investigate(state: ExchangeState) -> str:
    if state.get("error_code") == "ORDER_NOT_FOUND":
        return "explain_ineligible"
    return "handle_error" if state.get("error_code") else "retrieve_policy"


def _route_after_eligibility(state: ExchangeState) -> str:
    if state.get("error_code"):
        return "handle_error"
    eligible = (state.get("eligibility_result") or {}).get("eligible", False)
    return "search_catalog_node" if eligible else "explain_ineligible"


def _route_after_explain_ineligible(state: ExchangeState) -> str:
    return (
        "handle_error"
        if state.get("error_code") == "OUTPUT_VALIDATION_FAILED"
        else "update_ticket_node"
    )


def _route_after_search_catalog(state: ExchangeState) -> str:
    if state.get("error_code") in {"NO_CATALOG_RESULTS", "NO_SAME_CATEGORY_ITEM"}:
        return "explain_ineligible"
    return "check_catalog_node"


def _route_after_check_catalog(state: ExchangeState) -> str:
    if state.get("error_code") in {"NO_SAME_CATEGORY_ITEM", "NO_CATALOG_RESULTS"}:
        return "explain_ineligible"
    return "handle_error" if state.get("error_code") else "create_proposal"


def _route_after_create_proposal(state: ExchangeState) -> str:
    return "handle_error" if state.get("error_code") else "await_approval"


def _route_after_revalidate(state: ExchangeState) -> str:
    error = state.get("error_code")
    if error == "PROPOSAL_REJECTED":
        return "handle_rejection"
    if error:
        return "handle_error"
    return "execute_exchange"


def _route_after_execute_exchange(state: ExchangeState) -> str:
    return "handle_error" if state.get("error_code") else "update_ticket_node"


# ─── Graph assembly ────────────────────────────────────────────────────────────

def _build_exchange_graph() -> StateGraph:
    builder = StateGraph(ExchangeState)

    builder.add_node("investigate", investigate_node)
    builder.add_node("retrieve_policy", retrieve_policy_node)
    builder.add_node("check_eligibility", check_eligibility_node)
    builder.add_node("explain_ineligible", explain_ineligible_node)
    builder.add_node("search_catalog_node", search_catalog_node)
    builder.add_node("check_catalog_node", check_catalog_node)
    builder.add_node("create_proposal", create_proposal_node)
    builder.add_node("await_approval", await_approval_node)
    builder.add_node("revalidate", revalidate_node)
    builder.add_node("execute_exchange", execute_exchange_node)
    builder.add_node("handle_rejection", handle_rejection_node)
    builder.add_node("update_ticket_node", update_ticket_node)
    builder.add_node("handle_error", handle_error_node)

    builder.add_edge(START, "investigate")
    builder.add_conditional_edges("investigate", _route_after_investigate,
        {"retrieve_policy": "retrieve_policy", "handle_error": "handle_error",
         "explain_ineligible": "explain_ineligible"})
    builder.add_edge("retrieve_policy", "check_eligibility")
    builder.add_conditional_edges("check_eligibility", _route_after_eligibility,
        {"search_catalog_node": "search_catalog_node",
         "explain_ineligible": "explain_ineligible", "handle_error": "handle_error"})
    builder.add_conditional_edges("explain_ineligible", _route_after_explain_ineligible,
        {"update_ticket_node": "update_ticket_node", "handle_error": "handle_error"})
    builder.add_conditional_edges("search_catalog_node", _route_after_search_catalog,
        {"check_catalog_node": "check_catalog_node",
         "explain_ineligible": "explain_ineligible"})
    builder.add_conditional_edges("check_catalog_node", _route_after_check_catalog,
        {"create_proposal": "create_proposal", "handle_error": "handle_error",
         "explain_ineligible": "explain_ineligible"})
    builder.add_conditional_edges("create_proposal", _route_after_create_proposal,
        {"await_approval": "await_approval", "handle_error": "handle_error"})
    builder.add_edge("await_approval", "revalidate")
    builder.add_conditional_edges("revalidate", _route_after_revalidate,
        {"execute_exchange": "execute_exchange",
         "handle_rejection": "handle_rejection", "handle_error": "handle_error"})
    builder.add_conditional_edges("execute_exchange", _route_after_execute_exchange,
        {"update_ticket_node": "update_ticket_node", "handle_error": "handle_error"})
    builder.add_edge("handle_rejection", "update_ticket_node")
    builder.add_edge("update_ticket_node", END)
    builder.add_edge("handle_error", END)

    return builder.compile(checkpointer=MemorySaver())


exchange_workflow_graph = _build_exchange_graph()