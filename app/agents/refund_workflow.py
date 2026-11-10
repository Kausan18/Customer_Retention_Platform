"""
Refund workflow graph with LangGraph interrupt/resume.

Node flow:
  START → investigate → retrieve_policy → check_eligibility
        ↓ [ineligible]   explain_ineligible → update_ticket_node → END
        ↓ [eligible]     create_proposal → await_approval  ← INTERRUPT
                                                            ↓ RESUME
                                           revalidate
                                           ↓ [rejected]  handle_rejection → update_ticket_node → END
                                           ↓ [approved]  execute_refund   → update_ticket_node → END
                                           ↓ [error]     handle_error → END

Design notes:
- interrupt() lives in await_approval_node (separate from create_proposal_node)
  so create_proposal's state update (proposal_id) is checkpointed before the
  interrupt fires. On resume, the node restarts; proposal_id is already in state.
- LLM nodes: explain_ineligible, handle_rejection only.
  All routing, eligibility, proposal creation, execution are pure code.
- tool_authorizer is called inside execute_approved_refund, not here.
"""
import json
from typing import Optional, Any
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, END, START
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command  # noqa: F401 (Command imported for callers)
from pydantic import BaseModel

from app.db.database import execute as db_execute
from app.tools.order_tools import get_order, get_payment_status, get_return_status, get_recent_orders
from app.tools.policy_tools import search_policy_tool
from app.tools.ticket_tools import update_ticket
from app.tools.payment_tools import create_refund_proposal, execute_approved_refund
from app.services.eligibility import check_refund_eligibility
from app.services.gemini_client import call_gemini
from app.guardrails.output_validator import validate_output
from app.services.audit import log_event


# ─── LLM output schemas ───────────────────────────────────────────────────────

class IneligibleExplanationOutput(BaseModel):
    explanation: str
    policy_ref: str


class RejectionResponseOutput(BaseModel):
    explanation: str


# ─── State ────────────────────────────────────────────────────────────────────

class RefundState(TypedDict):
    # Inputs — set at graph creation time
    run_id: str
    ticket_id: str
    customer_id: str
    order_id: Optional[str]
    complaint_message: str
    # Facts gathered by investigate_node
    order: Optional[dict]
    payment: Optional[dict]
    return_record: Optional[dict]
    # Policy passages from RAG
    policy_passages: Optional[list]
    # Eligibility result from check_refund_eligibility()
    eligibility_result: Optional[dict]
    # Proposal DB row ID — set by create_proposal_node
    proposal_id: Optional[str]
    # Reviewer decision — set by await_approval_node after interrupt returns
    reviewer_decision: Optional[Any]
    # Execution result from mock payment service
    execution_result: Optional[dict]
    # Error routing
    error_code: Optional[str]
    error_message: Optional[str]
    # Final customer-facing response
    final_response: Optional[str]


# ─── Node implementations ─────────────────────────────────────────────────────

def investigate_node(state: RefundState) -> dict:
    """Gather order, payment, and return facts. All code — no LLM."""
    order_id = state.get("order_id")
    customer_id = state["customer_id"]

    if not order_id:
        recent = get_recent_orders(customer_id, 1)
        if not recent.get("success") or not recent.get("orders"):
            return {
                "error_code": "NO_ORDER_FOUND",
                "error_message": "No orders found for this customer.",
            }
        order_id = recent["orders"][0]["order_id"]

    order = get_order(order_id, customer_id)
    if not order.get("success"):
        return {"error_code": order["error_code"], "error_message": order.get("message", "")}

    payment = get_payment_status(order_id)
    return_record = get_return_status(order_id)

    return {
        "order_id": order_id,
        "order": order,
        "payment": payment if payment.get("success") else None,
        "return_record": return_record if return_record.get("success") else None,
    }


def retrieve_policy_node(state: RefundState) -> dict:
    """RAG lookup for refund policy passages."""
    result = search_policy_tool("refund eligibility return window conditions inspection")
    passages = result.get("passages", []) if result.get("success") else []
    return {"policy_passages": passages}


def check_eligibility_node(state: RefundState) -> dict:
    """Deterministic eligibility check. Pure code — no LLM."""
    order_id = state.get("order_id")
    if not order_id:
        return {
            "eligibility_result": {
                "eligible": False,
                "error_code": "NO_ORDER_FOUND",
                "reason": "No order ID in state.",
            }
        }
    return {"eligibility_result": check_refund_eligibility(order_id)}


def explain_ineligible_node(state: RefundState) -> dict:
    """LLM node: empathetic policy-grounded explanation of why refund is ineligible."""
    eligibility = state.get("eligibility_result", {})
    passages = state.get("policy_passages", [])
    policy_text = (
        "\n".join(
            f"[{p['document']}:{p['section']}] {p['content'][:300]}" for p in passages
        )
        if passages
        else "No policy passages retrieved."
    )

    prompt = f"""[SYSTEM]
You are a customer support agent for TechMart, an online electronics retailer.
Respond ONLY with valid JSON matching the schema below. No preamble, no markdown.
Do not invent facts. Base your response only on the provided context.

[CONTEXT]
Customer ID: {state['customer_id']}
Order ID: {state.get('order_id')}
Eligibility Result: {json.dumps(eligibility)}
Policy Passages:
{policy_text}

[TASK]
Explain to the customer why their refund request cannot be processed.
Reference the specific policy condition that was not met.
Be empathetic but clear. Do not suggest workarounds or exceptions not in the policy.

[OUTPUT FORMAT]
{{
  "explanation": "string — customer-facing explanation, 2-3 sentences",
  "policy_ref": "string — document:section that applies, e.g. refund_policy_v1:eligibility_conditions"
}}
"""
    raw = call_gemini(prompt)
    result, error = validate_output(raw, IneligibleExplanationOutput)
    if error:
        raw2 = call_gemini(
            f"{prompt}\n\nPrevious response failed validation: {error}\nPlease fix and retry."
        )
        result, error2 = validate_output(raw2, IneligibleExplanationOutput)
        if error2:
            return {
                "final_response": (
                    "We're unable to process your refund request at this time. "
                    "Please review our refund policy or contact support for assistance."
                ),
                "error_code": "OUTPUT_VALIDATION_FAILED",
            }

    return {"final_response": result.explanation}


def create_proposal_node(state: RefundState) -> dict:
    """
    Code node: create refund proposal in DB. Idempotent.
    State update (proposal_id) is checkpointed before await_approval_node fires interrupt().
    """
    eligibility = state["eligibility_result"]
    order_id = state["order_id"]
    run_id = state["run_id"]
    return_record = state.get("return_record") or {}
    passages = state.get("policy_passages") or []

    policy_refs = [
        f"{p['document']}:{p['section']}" for p in passages[:2]
    ]
    evidence_refs = [f"order_{order_id}"]
    if return_record.get("return_id"):
        evidence_refs.append(f"return_{return_record['return_id']}")

    proposal = create_refund_proposal(
        run_id=run_id,
        order_id=order_id,
        amount_minor=eligibility["refundable_amount_minor"],
        reason=eligibility["reason"],
        policy_refs=policy_refs,
        evidence_refs=evidence_refs,
    )

    if not proposal.get("success"):
        return {
            "error_code": "PROPOSAL_CREATION_FAILED",
            "error_message": "Failed to create refund proposal in database.",
        }

    return {"proposal_id": proposal["proposal_id"]}


def await_approval_node(state: RefundState) -> dict:
    """
    Interrupt node: pauses graph waiting for reviewer.
    First execution: interrupt() raises GraphInterrupt; state checkpointed.
    On resume via Command(resume={"decision": "approved"}): interrupt() returns that dict.
    """
    decision = interrupt(
        {
            "proposal_id": state["proposal_id"],
            "run_id": state["run_id"],
            "pending_action": "awaiting_approval",
        }
    )
    return {"reviewer_decision": decision}


def revalidate_node(state: RefundState) -> dict:
    """
    Re-check eligibility after reviewer decision. Branches on decision + re-validation.
    Runs AFTER interrupt resumes — financial side effects are in execute_refund_node.
    """
    reviewer_decision = state.get("reviewer_decision")

    # Decision arrives as the dict passed to Command(resume=...)
    if isinstance(reviewer_decision, dict):
        decision = reviewer_decision.get("decision", "rejected")
    else:
        decision = str(reviewer_decision) if reviewer_decision else "rejected"

    if decision == "rejected":
        return {"error_code": "PROPOSAL_REJECTED", "reviewer_decision": decision}

    # Re-run eligibility to catch edge cases (order status changed, etc.)
    re_check = check_refund_eligibility(state["order_id"])
    if not re_check.get("eligible"):
        return {
            "error_code": "ELIGIBILITY_CHANGED_AFTER_APPROVAL",
            "error_message": f"Order no longer eligible: {re_check.get('error_code')}",
            "reviewer_decision": decision,
        }

    return {"reviewer_decision": decision}


def execute_refund_node(state: RefundState) -> dict:
    """
    Code node: call execute_approved_refund (which runs tool_authorizer internally).
    Builds grounded final_response from confirmed payment data.
    """
    result = execute_approved_refund(state["proposal_id"], state["customer_id"])

    if not result.get("success"):
        return {
            "error_code": result.get("error_code", "REFUND_EXECUTION_FAILED"),
            "error_message": result.get("message", "Payment service returned an error."),
            "execution_result": result,
        }

    amount_rupees = state["eligibility_result"]["refundable_amount_minor"] / 100
    return {
        "execution_result": result,
        "final_response": (
            f"Your refund of ₹{amount_rupees:.2f} has been approved and submitted to the "
            f"payment provider. The amount will be credited to your original payment method "
            f"within 5–7 business days."
        ),
    }


def handle_rejection_node(state: RefundState) -> dict:
    """LLM node: explain reviewer rejection to customer."""
    amount_rupees = (
        state.get("eligibility_result", {}).get("refundable_amount_minor", 0) / 100
    )
    prompt = f"""[SYSTEM]
You are a customer support agent for TechMart. Respond ONLY with valid JSON.

[CONTEXT]
Customer ID: {state['customer_id']}
Order ID: {state.get('order_id')}
Refund amount requested: ₹{amount_rupees:.2f}

[TASK]
Inform the customer that their refund request was reviewed by our support team
and was not approved at this time. Be professional and empathetic.
Mention they can contact support if they have further questions.

[OUTPUT FORMAT]
{{"explanation": "string — 2-3 sentence customer-facing message"}}
"""
    raw = call_gemini(prompt)
    result, error = validate_output(raw, RejectionResponseOutput)
    if error:
        return {
            "final_response": (
                "Your refund request has been reviewed by our support team. "
                "At this time, we are unable to approve the refund. "
                "Please contact our support team for further assistance."
            )
        }
    return {"final_response": result.explanation}


def update_ticket_node(state: RefundState) -> dict:
    """Code node: write final_response to ticket summary and set resolved/escalated status."""
    final_response = state.get("final_response") or "Refund workflow completed."
    error_code = state.get("error_code")

    if error_code == "PROPOSAL_REJECTED":
        status = "resolved"
    elif error_code:
        status = "escalated"
        final_response = (
            state.get("error_message")
            or "An issue occurred processing your request. Our team has been notified."
        )
    else:
        exec_result = state.get("execution_result") or {}
        payment_status = exec_result.get("status", "")
        status = "resolved" if payment_status not in {"pending", "processing", ""} else "in_progress"

    update_ticket(state["ticket_id"], status, final_response, state["customer_id"])
    log_event(
        state["run_id"],
        "system",
        "workflow_completed",
        json.dumps({"final_status": status, "error_code": error_code}),
    )
    return {}


def handle_error_node(state: RefundState) -> dict:
    """Code node: escalate unexpected errors."""
    error_code = state.get("error_code", "UNKNOWN_ERROR")
    message = state.get("error_message") or "An unexpected error occurred."
    note = f"Workflow error: {error_code}. {message}"
    update_ticket(state["ticket_id"], "escalated", note, state["customer_id"])
    log_event(
        state["run_id"],
        "system",
        "workflow_error",
        json.dumps({"error_code": error_code, "message": message}),
    )
    return {
        "final_response": (
            "We encountered an issue processing your request. "
            "Our support team has been notified and will follow up with you."
        )
    }


# ─── Routing functions ─────────────────────────────────────────────────────────

def _route_after_investigate(state: RefundState) -> str:
    return "handle_error" if state.get("error_code") else "retrieve_policy"


def _route_after_eligibility(state: RefundState) -> str:
    if state.get("error_code"):
        return "handle_error"
    eligible = (state.get("eligibility_result") or {}).get("eligible", False)
    return "create_proposal" if eligible else "explain_ineligible"


def _route_after_explain_ineligible(state: RefundState) -> str:
    return (
        "handle_error"
        if state.get("error_code") == "OUTPUT_VALIDATION_FAILED"
        else "update_ticket_node"
    )


def _route_after_create_proposal(state: RefundState) -> str:
    return "handle_error" if state.get("error_code") else "await_approval"


def _route_after_revalidate(state: RefundState) -> str:
    error = state.get("error_code")
    if error == "PROPOSAL_REJECTED":
        return "handle_rejection"
    if error:
        return "handle_error"
    return "execute_refund"


def _route_after_execute_refund(state: RefundState) -> str:
    return "handle_error" if state.get("error_code") else "update_ticket_node"


# ─── Graph assembly ────────────────────────────────────────────────────────────

def _build_refund_graph() -> StateGraph:
    builder = StateGraph(RefundState)

    builder.add_node("investigate", investigate_node)
    builder.add_node("retrieve_policy", retrieve_policy_node)
    builder.add_node("check_eligibility", check_eligibility_node)
    builder.add_node("explain_ineligible", explain_ineligible_node)
    builder.add_node("create_proposal", create_proposal_node)
    builder.add_node("await_approval", await_approval_node)
    builder.add_node("revalidate", revalidate_node)
    builder.add_node("execute_refund", execute_refund_node)
    builder.add_node("handle_rejection", handle_rejection_node)
    builder.add_node("update_ticket_node", update_ticket_node)
    builder.add_node("handle_error", handle_error_node)

    builder.add_edge(START, "investigate")
    builder.add_conditional_edges(
        "investigate",
        _route_after_investigate,
        {"retrieve_policy": "retrieve_policy", "handle_error": "handle_error"},
    )
    builder.add_edge("retrieve_policy", "check_eligibility")
    builder.add_conditional_edges(
        "check_eligibility",
        _route_after_eligibility,
        {
            "create_proposal": "create_proposal",
            "explain_ineligible": "explain_ineligible",
            "handle_error": "handle_error",
        },
    )
    builder.add_conditional_edges(
        "explain_ineligible",
        _route_after_explain_ineligible,
        {"update_ticket_node": "update_ticket_node", "handle_error": "handle_error"},
    )
    builder.add_conditional_edges(
        "create_proposal",
        _route_after_create_proposal,
        {"await_approval": "await_approval", "handle_error": "handle_error"},
    )
    builder.add_edge("await_approval", "revalidate")
    builder.add_conditional_edges(
        "revalidate",
        _route_after_revalidate,
        {
            "execute_refund": "execute_refund",
            "handle_rejection": "handle_rejection",
            "handle_error": "handle_error",
        },
    )
    builder.add_conditional_edges(
        "execute_refund",
        _route_after_execute_refund,
        {"update_ticket_node": "update_ticket_node", "handle_error": "handle_error"},
    )
    builder.add_edge("handle_rejection", "update_ticket_node")
    builder.add_edge("update_ticket_node", END)
    builder.add_edge("handle_error", END)

    return builder.compile(checkpointer=MemorySaver())


# Module-level singleton — shared across all API calls in this process.
# MemorySaver persists interrupt state between POST /tickets/.../runs and
# POST /approvals/.../decision as long as the server stays up.
refund_workflow_graph = _build_refund_graph()