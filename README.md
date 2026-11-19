# Customer Support Agent

An AI customer support system for a fictional online electronics retailer (TechMart). Built as a portfolio project to demonstrate end-to-end agentic AI: LangGraph workflows, LLM guardrails, RAG policy retrieval, human-in-the-loop approval, and a measurable eval harness.

**Stack:** Python · LangGraph · FastAPI · SQLite · Streamlit · Gemini (gemini-3.5-flash-lite)

---

## What It Does

A customer submits a complaint. The agent classifies the intent, gathers facts from the database, retrieves the applicable policy via RAG, checks eligibility with deterministic code, and either answers directly or proposes an action. For financial actions (refunds and exchanges) the workflow pauses for a human reviewer to approve before anything executes. Every step is logged to an audit trail.

**Supported workflows:**

| Workflow | What it does |
|---|---|
| Order Placement | Catalog search → option presentation → customer confirmation → order creation |
| Order Tracking | Fetch shipment record → LLM explains status in natural language |
| Order Status | Fetch order + payment → LLM summarizes current state |
| Refund Request | Eligibility check → proposal → human approval → mock payment → verified outcome |
| Exchange Request | Eligibility check → same-category catalog search → proposal → human approval → new order |

---

## Scope

**In scope for V1:**
- 5 customer workflows listed above
- Single authenticated customer session per workflow run
- Mock payment and shipping services (configurable for success, failure, timeout, delay, pending)
- RAG over 5 synthetic policy documents (refund, exchange, return processing, damaged item, shipping delay)
- Human approval required for all refund and exchange actions
- 5 guardrail types: input screening, intent validation, structured output validation, tool call authorization, response grounding
- SQLite database (99,457 CSV customer records + synthetic orders/payments/returns)
- Streamlit demo UI
- 30-case eval harness with 5 measured metrics

**Out of scope for V1:**
- Multi-agent architecture
- PostgreSQL / pgvector
- Idempotency keys and payment reconciliation across server restarts
- Fraud detection, chargebacks, partial returns
- Multiple stores or currencies
- Real payment processing

---

## Architecture

```
Streamlit UI (streamlit_app.py)
       │  HTTP via requests
       ▼
FastAPI (app/main.py — uvicorn)
  ├── POST /auth/login, /auth/reviewer-login, /auth/logout
  ├── POST /tickets                    ← input screener + rate limiter
  ├── POST /tickets/{id}/runs          ← intent validator
  ├── GET  /runs/{id}
  ├── GET  /runs/{id}/events
  ├── GET  /approvals
  ├── POST /approvals/{id}/decision
  └── POST /runs/{id}/messages         ← order_placement customer reply
       │
       ▼
Guardrails Layer
  ├── input_screener.py      — injection patterns, length, cross-customer block
  ├── intent_validator.py    — 5 supported intents, out-of-scope detection
  ├── output_validator.py    — Pydantic schema validation + re-prompt once
  ├── tool_authorizer.py     — proposal existence, approval, expiry, hash tamper check
  └── response_grounder.py   — cross-customer ID, refund confirmation, amount match
       │
       ▼
LangGraph Workflow Router (app/api/tickets.py dispatcher)
  ├── order_placement_graph   — interrupt/resume for customer confirmation
  ├── order_tracking_graph    — read-only, no interrupt
  ├── order_status_graph      — read-only, no interrupt
  ├── refund_workflow_graph   — interrupt/resume for reviewer approval
  └── exchange_workflow_graph — interrupt/resume for reviewer approval
       │
       ▼
Tools Layer (app/tools/)
  ├── order_tools.py     — get_order, get_recent_orders, get_shipment, get_payment_status, get_return_status
  ├── catalog_tools.py   — search_catalog, create_exchange_proposal, execute_approved_exchange, place_order
  ├── payment_tools.py   — create_refund_proposal, execute_approved_refund
  ├── shipping_tools.py  — get_tracking_info
  ├── policy_tools.py    — search_policy_tool (RAG)
  └── ticket_tools.py    — update_ticket
       │
       ▼
SQLite (data/customer_support.sqlite3)
  ├── orders, order_items, shipments, payments, returns
  ├── product_catalog
  ├── tickets, workflow_runs
  ├── action_proposals, approvals
  ├── audit_events
  └── policy_documents, policy_chunks (RAG)
       │
       ▼
Mock External Services (mock_services/)
  ├── payment_service.py  — idempotency store, configurable failure modes
  └── shipping_service.py — configurable status modes
```

**Key architectural note — MemorySaver is process-scoped.** LangGraph interrupt/resume state lives in the FastAPI process's memory. Streamlit communicates with FastAPI exclusively via HTTP (no direct imports). FastAPI must remain running between POST /runs and POST /approvals/decision — a server restart loses checkpoint state. This is a known V1 limitation; V2 upgrades to a PostgreSQL-backed checkpointer.

---

## Setup

### Requirements

- Python 3.10+
- A Google Gemini API key with access to `gemini-3.5-flash-lite`

### Install

```powershell
git clone <repo>
cd customer-support-agent
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Configure

```powershell
Copy-Item .env.example .env
```

Edit `.env`:

```
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_TIMEOUT_SECONDS=120
DEMO_PIN=1234
```

### Seed the database

```powershell
python -m app.db.seed
python verify_seed.py
```

Expected output: 200 orders, 30 products, ~27 returns, all integrity checks passing.

### Ingest policies

```powershell
python rag/ingest_policies.py
```

Expected output: 5 documents, 20 chunks ingested.

### Run FastAPI

```powershell
uvicorn app.main:app --reload
```

FastAPI runs at `http://127.0.0.1:8000`. Keep this terminal open.

### Run Streamlit

In a second terminal:

```powershell
.venv\Scripts\Activate.ps1
streamlit run streamlit_app.py
```

Streamlit opens at `http://localhost:8501`.

---

## Demo Scenarios

Three scenarios demonstrate the system's key behaviors. Use the sidebar "Demo Scenarios" selector to pre-fill customer credentials and complaint text automatically.

### Demo A — Successful Approved Refund

1. The strategy query finds a delivered order with a passed-inspection return within 30 days.
2. The customer submits a refund complaint.
3. The refund workflow investigates, retrieves the refund policy, checks eligibility, and creates a proposal.
4. The workflow pauses at Panel 4 — Approval Queue.
5. The reviewer sees the evidence and policy citations and clicks Approve.
6. The workflow resumes, calls the mock payment service, verifies the result, and updates the ticket.
7. Panel 5 shows the confirmed refund response.

**What it proves:** End-to-end agentic flow, RAG policy retrieval, interrupt/resume approval, idempotent mock payment, audit trail.

### Demo B — Correctly Blocked Refund

1. The strategy query finds a delivered order outside the 30-day refund window.
2. The customer submits a refund complaint.
3. The workflow investigates, retrieves the refund policy, runs the eligibility check, and determines `OUTSIDE_WINDOW`.
4. No proposal is created. The workflow takes the ineligible branch.
5. Panel 5 shows the response citing the 30-day policy limit.

**What it proves:** Deterministic eligibility enforcement in code (not LLM), RAG citation in customer response, ineligible path completes cleanly.

### Demo C — Cross-Category Exchange Decline

1. The strategy query finds an eligible exchange order (delivered within 15 days).
2. The customer message requests an exchange for a product in a different category (e.g. Electronics → Shoes).
3. `extract_requested_category()` detects the category mismatch before the catalog search runs.
4. The workflow takes the ineligible branch with `NO_SAME_CATEGORY_ITEM`.
5. Panel 5 shows the response explaining the same-category requirement.

**What it proves:** Pre-search category enforcement (not fuzzy LLM routing), exchange policy citation, early exit on business rule violation.

---

## Tool Contracts

| Tool | Inputs | Authorization |
|---|---|---|
| `get_order` | `order_id`, `customer_id` | `customer_id` from session — not from model or request body |
| `get_recent_orders` | `customer_id`, `limit` | `customer_id` from session |
| `get_shipment` | `order_id` | Ownership established by prior `get_order` call |
| `get_payment_status` | `order_id` | Ownership established by prior `get_order` call |
| `get_return_status` | `order_id` | Ownership established by prior `get_order` call |
| `search_catalog` | `query`, `category` | None — read-only catalog |
| `place_order` | `customer_id`, `items[]` | `customer_id` from session; prices re-read from catalog |
| `search_policy_tool` | `query`, `document_id` | None — read-only policy chunks |
| `create_refund_proposal` | `order_id`, `amount_minor`, `reason` | Eligibility pre-checked in code |
| `execute_approved_refund` | `proposal_id` | tool_authorizer: proposal exists, status=approved, approval decided, not expired, hash matches |
| `create_exchange_proposal` | `order_id`, `reason`, `replacement_product_id` | Eligibility pre-checked in code |
| `execute_approved_exchange` | `proposal_id` | Same 5-check tool_authorizer as refund |
| `update_ticket` | `ticket_id`, `status`, `note`, `customer_id` | Ownership checked before write |

The model never supplies reviewer identity, approval status, or payment credentials. All privileged parameters are server-controlled.

---

## Approval Design

The interrupt/resume approval lifecycle:

1. `create_proposal_node` writes an immutable `action_proposals` row with SHA256 payload hash and 24-hour expiry. Creates a linked `approvals` row with `decision=NULL`.
2. `await_approval_node` calls LangGraph's `interrupt()`. The workflow checkpoints and pauses.
3. FastAPI returns the run as `status: paused`. Streamlit displays Panel 4.
4. `GET /approvals` returns the pending proposal with full evidence and policy citation context to the reviewer.
5. Reviewer login creates a role-scoped session; `GET /approvals` and `POST /approvals/{id}/decision` require its Bearer token. The decision endpoint validates the proposal is not expired and not already decided, records the session reviewer, and resumes the graph with `Command(resume=decision)`.
6. The resumed graph runs `revalidate_node`: re-checks eligibility and balance, verifies the payload hash matches what was approved.
7. Only then does `execute_node` call the payment or exchange service.

An approval message in chat is not sufficient authorization. The execution tool verifies the stored decision independently of the graph state.

---

## RAG Setup

Five synthetic policy documents in `policies/`:

| Document | Key rule |
|---|---|
| `refund_policy_v1.md` | 30-day refund window from delivery; inspection must pass; reviewer approval required |
| `exchange_policy_v1.md` | 15-day exchange window; same product category only; reviewer approval required |
| `return_processing_v1.md` | Returns inspected within 3–5 business days of receipt |
| `damaged_item_policy_v1.md` | Report within 7 days; photo evidence required |
| `shipping_delay_policy_v1.md` | Delay = 3+ days past estimated delivery; compensation options |

Chunks are split on `##` headings (~300 tokens each). Embeddings generated with `sentence-transformers/all-MiniLM-L6-v2`, normalized for cosine similarity via dot product. Stored in SQLite as JSON blobs. Retrieval returns top-3 passages with document and section citations. Policy versions and effective dates are stored in `policy_documents` metadata.

---

## Eval Methodology and Results

### Harness design

`evals/run_evals.py` submits each case through the full HTTP API stack — no direct function calls, no mocking. Customer strategies resolve to live DB rows at runtime (re-seed-safe). Each case logs in, creates a ticket, starts a run, optionally supplies a reviewer decision or customer reply, then asserts on `run_status`, `ticket_status`, and DB row state.

### Results

| Metric | Result | Target |
|---|---|---|
| Executed / Total | 27 / 30 | — |
| Verified resolution rate | 1.0 (100%) | ≥ 80% |
| Guardrail precision | 1.0 (100%) | ≥ 95% |

3 cases skipped (GR-07, GR-08, GR-09): these test LLM output injection behaviors (malformed schema, cross-customer ID leakage, premature refund confirmation) that cannot be triggered reliably through the public HTTP API without mocking the Gemini client. Each is covered by unit tests in `tests/test_guardrails.py`. See build log Session 8 for full skip documentation and V2 remediation plan.

### Case breakdown

| Group | Cases | Notes |
|---|---|---|
| Order placement | EP-01 to EP-05 | Includes guardrail block for bulk order |
| Order tracking + status | ET-01 to ET-05 | Includes cross-customer auth block |
| Refund | ER-01 to ER-05 | Includes rejection path and guardrail block |
| Exchange | EX-01 to EX-05 | Includes cross-category decline and expired approval |
| Guardrails | GR-01 to GR-10 | 7 executed, 3 skipped |

Full results: `evals/reports/v1_results.json`

---

## Known Limitations

**MemorySaver is process-scoped.** LangGraph interrupt/resume state is held in the FastAPI process's memory. If `uvicorn` is restarted between POST /runs and POST /approvals/decision, the checkpoint is lost and the workflow cannot resume. V2 replaces MemorySaver with a PostgreSQL-backed LangGraph checkpointer for durable state.

**GR-07/08/09 not end-to-end tested.** Output validator re-prompt behavior, cross-customer ID leakage detection, and premature refund confirmation detection are tested via unit tests only. End-to-end testing requires an injectable LLM interface to force specific model outputs, deferred to V2.

**Gemini availability.** `gemini-3.5-flash-lite` availability, rate limits, and latency depend on the Google Gemini API account. The app sets `GEMINI_TIMEOUT_SECONDS` (default 120) and surfaces timeout errors explicitly.

**No persistent sessions.** Session tokens are held in process memory. Restarting FastAPI invalidates all active sessions — customers must re-login.

**Single currency, single store.** All amounts are in INR (paise). Multi-currency and multi-store support are out of V1 scope.

---

## Repository Structure

```
customer-support-agent/
├── app/
│   ├── main.py                    ← FastAPI app + base-version chatbot
│   ├── api/
│   │   ├── tickets.py             ← POST /tickets, POST /tickets/{id}/runs
│   │   ├── runs.py                ← GET /runs/{id}, GET /runs/{id}/events, POST /runs/{id}/messages
│   │   └── approvals.py           ← GET /approvals, POST /approvals/{id}/decision
│   ├── auth/
│   │   └── session.py             ← token registry, require_session dependency
│   ├── schemas/
│   │   ├── ticket.py
│   │   ├── proposal.py
│   │   ├── approval.py
│   │   └── workflow_state.py      ← LangGraph node output schemas
│   ├── agents/
│   │   ├── order_placement.py
│   │   ├── order_tracking.py
│   │   ├── order_status.py
│   │   ├── refund_workflow.py
│   │   └── exchange_workflow.py
│   ├── tools/
│   │   ├── order_tools.py
│   │   ├── catalog_tools.py
│   │   ├── payment_tools.py
│   │   ├── shipping_tools.py
│   │   ├── policy_tools.py
│   │   └── ticket_tools.py
│   ├── guardrails/
│   │   ├── input_screener.py
│   │   ├── intent_validator.py
│   │   ├── output_validator.py
│   │   ├── tool_authorizer.py
│   │   └── response_grounder.py
│   ├── services/
│   │   ├── eligibility.py
│   │   ├── audit.py
│   │   ├── gemini_client.py
│   │   └── rate_limiter.py
│   └── db/
│       ├── database.py
│       └── seed.py
├── mock_services/
│   ├── payment_service.py
│   └── shipping_service.py
├── rag/
│   ├── embedder.py
│   ├── retriever.py
│   └── ingest_policies.py
├── policies/
│   ├── refund_policy_v1.md
│   ├── exchange_policy_v1.md
│   ├── return_processing_v1.md
│   ├── damaged_item_policy_v1.md
│   └── shipping_delay_policy_v1.md
├── evals/
│   ├── eval_cases.json
│   ├── run_evals.py
│   └── reports/
│       └── v1_results.json
├── tests/
│   ├── test_guardrails.py        ← 25 tests, all 5 guardrail types
│   ├── test_eligibility.py       ← 13 tests, refund + exchange rules
│   ├── test_order_placement.py   ← 5 tests, place_order() tool
│   └── test_exchange_workflow.py ← 4 tests, category extraction
├── streamlit_app.py
├── verify_seed.py
├── data/
│   ├── customer_shopping_data.csv   ← source CSV, never modified
│   └── customer_support.sqlite3     ← created by seed.py
├── .env.example
├── requirements.txt
└── README.md
```

---

## Running Tests

```powershell
python -m pytest tests/ -v
```

Expected: 47 tests passing (25 guardrails + 13 eligibility + 5 order placement + 4 exchange workflow).

Run the eval harness separately (requires FastAPI running):

```powershell
# Terminal 1
uvicorn app.main:app --reload

# Terminal 2
python evals/run_evals.py
```

---

## Résumé Entry

> Built a customer support resolution system using Python, LangGraph, FastAPI, and SQLite, integrating RAG policy retrieval over 5 synthetic documents, human-approved refund and exchange workflows with interrupt/resume, 5 guardrail types (input screening, intent validation, structured output validation, tool authorization, response grounding), and an auditable ticket trail.
>
> Achieved 100% verified resolution rate on 27 of 30 held-out synthetic cases; 3 cases cover LLM output injection behaviors tested via unit tests. Validated duplicate-refund prevention via idempotency store across retry and concurrent-request scenarios.

---

## Environment Variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GEMINI_API_KEY` | Yes | — | Google Gemini API key |
| `GEMINI_MODEL` | No | `gemini-3.5-flash-lite` | Model name |
| `GEMINI_TIMEOUT_SECONDS` | No | `120` | Request timeout |
| `DEMO_PIN` | No | `1234` | Shared customer login PIN |