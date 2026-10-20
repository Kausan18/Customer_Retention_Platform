## Customer Support Agent


# A practical Agentic AI Project

**Stack:** Python, LangGraph, FastAPI, PostgreSQL

**Project level:** Intermediate

**Suggested timeline:** 3–4 weeks, depending on your experience

**What you’ll build:** A support system that investigates customer problems, retrieves relevant policies, proposes solutions, executes approved actions, and records the outcome.

---

## 1. The project in one sentence

Build an AI agent that takes a customer complaint from **“What happened?”** to **“Here’s the verified outcome”**, using business data, support policies, APIs, and human approval.

A typical interaction:

> **Customer:** “I returned my headphones last week, but I still haven’t received my refund.”
> 

Your system should:

1. Identify the authenticated customer.
2. Retrieve the order, return, and payment records.
3. Find the applicable refund policy.
4. Determine whether a refund is eligible, already processing, or blocked.
5. Propose an appropriate action.
6. Request human approval before issuing a new refund.
7. Call the refund API after approval and validation.
8. Update the support ticket.
9. Explain what happened without claiming more than the API confirmed.

**Use synthetic customer data and a mock payment service throughout the initial project.**

---

## 2. Why this is relevant to FDE roles

Forward Deployed Engineering involves translating an actual customer workflow into a working technical system.

This project gives you a way to demonstrate that process:

| Skill | How the project demonstrates it |
| --- | --- |
| Understanding business requirements | Translate support policies into workflows and eligibility rules |
| Integrating systems | Connect customer, order, payment, and ticket APIs |
| Building AI workflows | Combine retrieval, reasoning, tool calls, and structured outputs |
| Managing risk | Add authorization, approval, and transaction controls |
| Handling messy conditions | Recover from missing information, timeouts, and duplicate requests |
| Measuring business value | Evaluate verified resolution, escalation, latency, and cost |
| Communicating tradeoffs | Explain why some steps use an LLM and others use ordinary code |

**The strongest portfolio story is how reliably the system handles a customer problem, including when it should stop and ask for help.**

---

## 3. Define a manageable first version

Build for a fictional online electronics retailer.

Support three complaint types:

### A. Order status

Retrieve the order and shipment status, explain the latest available information, and update the ticket.

### B. Refund request

Check eligibility, prepare a refund proposal, obtain approval, execute the approved refund, and verify the result.

### C. Damaged item

Retrieve the order and relevant policy, collect required information, and prepare a resolution or escalate.

For the first version:

- Use one store and one currency.
- Require human approval for every refund.
- Mock shipping and payment services.
- Support one customer issue per workflow.
- Keep external customer messaging inside your demo interface.
- Exclude chargebacks, fraud adjudication, and complex partial returns.

Write these boundaries in your README so reviewers understand the intended scope.

---

## 4. Design the workflow before choosing agents

Use this flow as the foundation:

```
Customer submits complaint
          ↓
Authenticate and validate request
          ↓
Classify complaint
          ↓
Retrieve customer/order facts + relevant policies
          ↓
Check for missing information
          ↓
Create proposed resolution
          ↓
Validate against business rules
          ↓
   ┌──────┼──────────────┐
   ↓      ↓              ↓
Answer  Request       Escalate
only    approval      to human
          ↓
Approve / reject / expire
          ↓
Revalidate approved action
          ↓
Execute tool call
          ↓
Verify external result
          ↓
Update ticket and audit trail
          ↓
Return accurate customer update
```

**Important distinction:** The model proposes actions. Your application determines whether those actions are permitted and executes them through controlled tools.

---

## 5. Choose an architecture

Use four main components.

### FastAPI application

Handles:

- Customer requests.
- Authentication and authorization.
- Workflow creation.
- Status queries.
- Reviewer decisions.
- Input and response validation.

### LangGraph workflow

Coordinates:

- Classification.
- Investigation.
- Policy retrieval.
- Resolution proposals.
- Approval pauses.
- Execution and verification.
- Escalation.

### PostgreSQL

Stores:

- Customers and orders.
- Tickets and workflow runs.
- Policy metadata and chunks.
- Approval records.
- Refund operations.
- Audit events.
- Durable workflow checkpoints.

Keep application tables and LangGraph checkpoint tables logically separate.

### Mock external services

Simulate:

- Payment/refund API.
- Shipping API.
- Ticketing API.

Make these services capable of returning errors and delayed results so you can test realistic failure conditions.

**Optional:** Use pgvector to store and search policy embeddings inside PostgreSQL. It provides vector similarity search without requiring a separate vector database. See the pgvector documentation.

---

## 6. Implement a small multi-agent workflow

Start with one tool-using agent and deterministic control flow. Once it works, introduce specialist agents with distinct responsibilities.

A useful final design has three specialists:

| Agent | Responsibility | Output |
| --- | --- | --- |
| Investigation agent | Gather customer, order, return, and payment facts | Structured evidence summary |
| Policy agent | Retrieve applicable policy passages | Citations and policy interpretation |
| Resolution agent | Combine facts and policy into a proposed action | Structured resolution proposal |

Use ordinary code for:

- Authentication.
- Access control.
- Amount calculations.
- Eligibility enforcement.
- Approval validation.
- API execution.
- Retry limits.
- State transitions.

Give each specialist only the tools it needs. For example, the investigation agent should not have a refund execution tool.

If investigation and policy retrieval are independent, run them concurrently and combine their outputs. If policy selection depends on the product or order date, gather those facts first.

**Portfolio experiment:** Compare the specialist design against your single-agent baseline. Measure whether the additional coordination improves correctness enough to justify its cost and latency.

---

## 7. Design the database

A suggested schema:

| Table | Important fields |
| --- | --- |
| `customers` | `id`, `external_auth_subject`, `name`, `email` |
| `orders` | `id`, `customer_id`, `status`, `currency`, `paid_amount_minor`, `delivered_at` |
| `order_items` | `id`, `order_id`, `product_id`, `quantity`, `paid_amount_minor` |
| `returns` | `id`, `order_id`, `status`, `received_at`, `inspection_status` |
| `tickets` | `id`, `customer_id`, `order_id`, `category`, `status`, `summary` |
| `workflow_runs` | `id`, `ticket_id`, `thread_id`, `status`, `started_at`, `completed_at` |
| `policy_documents` | `id`, `version`, `effective_from`, `effective_to`, `source_uri` |
| `policy_chunks` | `id`, `document_id`, `content`, `embedding`, `metadata` |
| `action_proposals` | `id`, `run_id`, `action_type`, `payload`, `payload_hash`, `status` |
| `approvals` | `id`, `proposal_id`, `reviewer_id`, `decision`, `expires_at`, `decided_at` |
| `refund_operations` | `id`, `order_id`, `proposal_id`, `amount_minor`, `idempotency_key`, `provider_reference`, `status` |
| `audit_events` | `id`, `run_id`, `actor`, `event_type`, `redacted_payload`, `created_at` |

### Database rules worth implementing

- Store money in integer minor units, such as cents or paise.
- Add a unique constraint on each refund operation’s idempotency key.
- Track successful refunds and amounts reserved by pending operations.
- Prevent total committed refunds from exceeding the refundable balance.
- Record approval timestamps and the exact proposal approved.
- Use migrations so another developer can reproduce the schema.

**A workflow checkpoint is not your financial ledger.** Keep refund accounting in application tables with explicit constraints and transactions.

---

## 8. Build the tools and API integrations

Define narrow tools with typed inputs and structured outputs.

### Read tools

```
get_order(order_id)
get_return_status(order_id)
get_payment_status(order_id)
get_refund_status(operation_id)
search_policy(query, policy_filters)
get_ticket(ticket_id)
```

### Write tools

```
create_refund_proposal(order_id, amount_minor, reason)
execute_approved_refund(proposal_id)
update_ticket(ticket_id, status, note)
```

### Tool design principles

**Derive identity from the authenticated request.**

Do not let the model choose which customer it represents.

**Check ownership inside the service.**

An order ID provided by a customer or model does not establish access.

**Keep privileged parameters server-controlled.**

The model should not supply reviewer identity, approval status, or payment credentials.

**Return structured failures.**

Example:

```
{
  "success": false,
  "error_code": "RETURN_NOT_RECEIVED",
  "retryable": false,
  "message": "The return has not been marked as received."
}
```

**Separate proposals from execution.**

Preparing a refund request must not issue money.

---

## 9. Add RAG for support policies

Use retrieval for policy documents. Use APIs or SQL for current customer and transaction facts.

For example:

- “What is the return window?” → policy retrieval.
- “When was this order delivered?” → order API.
- “Has this payment already been refunded?” → payment API.

### Retrieval pipeline

1. Write a small set of synthetic policy documents.
2. Add document IDs, versions, and effective dates.
3. Split documents into meaningful sections.
4. Generate embeddings.
5. Store chunks and metadata.
6. Retrieve passages using the complaint and relevant order facts.
7. Return citations with the proposed resolution.

Start with policies covering:

- Refund eligibility.
- Return processing.
- Damaged products.
- Shipping delays.
- Exceptions requiring human review.

**Example policy — invented for this project:**

> Refunds for returned items become eligible after the return is received and inspection passes. A support reviewer must approve every refund.
> 

### Retrieval behavior to implement

- Filter to the applicable policy version.
- Preserve document and section references.
- Report when evidence is missing or conflicting.
- Escalate if no applicable policy can be established.
- Treat instructions embedded in retrieved text as untrusted content.

Do not use a retrieval similarity score as proof that a refund is valid. Enforce key financial conditions in code.

---

## 10. Define structured workflow state

Keep enough state to inspect and resume the workflow.

Suggested fields:

```
run_id
ticket_id
authenticated_customer_id
order_id
complaint_category
customer_message
verified_facts
policy_references
missing_information
proposed_action_id
approval_id
execution_status
external_result_reference
retry_counts
error_details
final_response
```

Use validated schemas for specialist outputs.

For a resolution proposal, require:

```
{
  "action": "request_refund_approval",
  "order_id": "ord_1042",
  "amount_minor": 249900,
  "currency": "INR",
  "reason": "Return received and inspection passed",
  "evidence_refs": ["return_ret_42", "payment_pay_92"],
  "policy_refs": ["refund_policy_v1:section_3"],
  "missing_information": []
}
```

These values are illustrative. The application must independently validate the amount, ownership, and eligibility.

---

## 11. Implement human approval correctly

Build a reviewer screen showing:

- Customer and order.
- Complaint summary.
- Proposed action and amount.
- Relevant evidence.
- Policy citations.
- Approve and reject controls.
- Proposal expiration time.

### Approval lifecycle

1. Persist an immutable proposal.
2. Create a pending approval bound to that proposal.
3. Pause the workflow.
4. Authenticate the reviewer.
5. Validate reviewer permissions.
6. Save the decision.
7. Resume the same workflow.
8. Recheck eligibility and refundable balance.
9. Execute only the approved action.

If the order, amount, or other material details change, invalidate the old approval and request a new one.

LangGraph supports this pause/resume pattern using `interrupt()`, a checkpointer, and `Command(resume=...)` with the same thread ID. The interrupted node restarts when resumed, so code before the interrupt may execute again. Keep financial side effects in a separate execution step. See LangGraph interrupts.

**An approval message in chat is not sufficient authorization.** The execution service must verify the stored approval and authenticated reviewer decision.

---

## 12. Make refunds safe to retry

A difficult but valuable test case:

> The payment service accepts the refund, but your application times out before receiving the response.
> 

Blindly retrying with a new request can create a duplicate refund.

### Suggested execution pattern

1. Create a durable refund operation with a stable idempotency key.
2. In a short database transaction, validate the approval and reserve the refundable amount.
3. Commit before calling the external service.
4. Call the provider with the stable key.
5. Persist its response.
6. If the result is uncertain, mark the operation `unknown` or `pending_reconciliation`.
7. Query the provider by key or reference before deciding what to do next.
8. Retry using the same key only when the provider’s contract supports it.

Your mock payment API should implement this contract: repeated requests with the same key and payload return the same operation; a changed payload is rejected.

For concurrent requests, use database locking or equivalent transactional controls when checking and reserving balances. PostgreSQL’s `SELECT ... FOR UPDATE` can protect rows against conflicting updates within a transaction. Avoid holding that transaction open during a network call. See PostgreSQL locking.

A checkpoint alone does not guarantee that an external action occurs exactly once.

---

## 13. Expose the workflow through FastAPI

Suggested endpoints:

| Endpoint | Purpose |
| --- | --- |
| `POST /tickets` | Create a customer support ticket |
| `POST /tickets/{ticket_id}/runs` | Start a resolution workflow |
| `GET /runs/{run_id}` | Retrieve status and output |
| `GET /runs/{run_id}/events` | Retrieve a redacted event timeline |
| `GET /approvals` | List approvals available to the reviewer |
| `POST /approvals/{approval_id}/decision` | Approve or reject a proposal |
| `POST /runs/{run_id}/messages` | Supply additional customer information |
| `GET /health` | Check application health |

Check authorization on every endpoint.

### Execution approach

For the earliest prototype, run short workflows directly and return when they finish or pause.

For a more robust version:

- Persist a job.
- Return `202 Accepted` with its run ID.
- Execute through a worker.
- Persist progress.
- Let clients query status.

Checkpointing enables state recovery, but you still need a mechanism to schedule and restart unfinished jobs. FastAPI documents `BackgroundTasks` for smaller in-process work and points to worker systems such as Celery for larger background workloads. See FastAPI background tasks.

---

## 14. Handle failures explicitly

| Failure | Expected behavior |
| --- | --- |
| Missing order information | Ask a targeted clarification question |
| Order belongs to another customer | Deny access without revealing its details |
| No applicable policy found | Escalate rather than invent eligibility |
| Conflicting policy evidence | Flag the conflict for review |
| Temporary read API failure | Retry with a bounded backoff policy |
| Invalid model output | Validate, attempt limited repair, then stop or escalate |
| Approval rejected | Record the decision and follow the rejection path |
| Approval expired | Require a new valid approval |
| Refund outcome uncertain | Reconcile before retrying or claiming success |
| Refund succeeds, ticket update fails | Retry only the ticket update |
| Worker crashes | Resume from persisted state and reconcile side effects |
| Excessive workflow loops | Stop at a configured step or cost limit |

Customer-facing language must match the actual state:

> “Your refund request is awaiting approval.”
> 

> “The payment service accepted your refund request. Processing is still pending.”
> 

> “I couldn’t confirm the refund outcome yet. The case is being checked.”
> 

Keep `submitted`, `processing`, `succeeded`, and `failed` distinct.

---

## 15. Evaluate verified outcomes

Build an evaluation harness that seeds a scenario, runs the workflow, supplies any simulated reviewer decision, and checks resulting system state.

Create around **60 synthetic cases**:

| Category | Suggested cases |
| --- | --- |
| Order status and shipment questions | 10 |
| Eligible refund requests | 10 |
| Ineligible refund requests | 10 |
| Missing information and ambiguous complaints | 10 |
| API failures, retries, and crashes | 10 |
| Authorization, concurrency, and adversarial inputs | 10 |

Keep a held-out subset that you do not use while tuning prompts.

### Store an expected outcome for each case

Include:

- Relevant customer and order.
- Applicable policy.
- Allowed actions.
- Forbidden actions.
- Whether approval is required.
- Expected final business state.
- Expected escalation or clarification behavior.

### Track these metrics

**Verified resolution rate**

Cases whose expected business outcome is confirmed, divided by cases designated resolvable in the test set.

**Overall workflow correctness**

Cases handled according to their expected outcome—including correct clarification, refusal, and escalation—divided by all cases.

**Unauthorized action count**

Refunds or data accesses performed without the required authorization.

**Duplicate refund count**

Duplicate financial effects under retries, restarts, and concurrent execution.

**Policy retrieval quality**

Whether the required policy evidence appears in the retrieved results.

**Grounded response quality**

Whether factual claims are supported by retrieved records and tool results.

**Operational metrics**

Latency, model calls, tool calls, retry count, and estimated cost per case.

Measure active execution time separately from time waiting for a reviewer.

Use database assertions and mock-provider records for transactional outcomes. An LLM judge can assist with response quality, but should not decide whether money moved correctly.

**Report human-assisted resolution separately from fully automated resolution.**

---

## 16. Build it in phases

### Week 1: Business workflow and backend

Build:

- Synthetic policies and customer records.
- PostgreSQL schema and migrations.
- Customer authentication.
- Ticket endpoints.
- Mock order, shipping, and payment APIs.
- Deterministic refund eligibility checks.

**Done when:** You can manually resolve a sample ticket through the APIs without an LLM.

### Week 2: Agent and retrieval

Build:

- Policy ingestion and retrieval.
- Typed tools.
- Complaint classification.
- Investigation and proposal generation.
- LangGraph state and routing.

**Done when:** The system gathers evidence and produces a validated proposal with citations.

### Week 3: Approval and reliability

Build:

- Reviewer interface.
- Durable approval pause/resume.
- Idempotent refund operations.
- Retry and reconciliation behavior.
- Ticket updates and audit trail.

**Done when:** Restarting or retrying a run does not issue duplicate refunds.

### Week 4: Evaluation and presentation

Build:

- Evaluation dataset and harness.
- Baseline comparisons.
- Failure-case tests.
- Simple demo interface.
- README, architecture diagram, and demo recording.

**Done when:** Another developer can run the project and reproduce your reported results.

---

## 17. Organize the repository

```
customer-support-resolution-agent/
├── app/
│   ├── main.py
│   ├── api/
│   ├── auth/
│   ├── schemas/
│   ├── agents/
│   ├── workflows/
│   ├── tools/
│   ├── services/
│   ├── policies/
│   └── db/
├── mock_services/
│   ├── payments/
│   └── shipping/
├── migrations/
├── scripts/
│   ├── seed_data.py
│   └── ingest_policies.py
├── evals/
│   ├── datasets/
│   ├── run_evals.py
│   └── reports/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── failure_scenarios/
├── docs/
├── docker-compose.yml
├── .env.example
├── pyproject.toml
└── README.md
```

Pin dependencies in a lockfile. Document model configuration, embedding configuration, required environment variables, and setup commands.

---

## 18. Create a demo that proves the system works

Show three scenarios:

### Demo A: Successful approved refund

- Customer submits a complaint.
- Agent retrieves records and policy.
- Reviewer sees the evidence and approves.
- Mock provider records the refund.
- Ticket receives the verified status.

### Demo B: Correctly blocked request

- Customer requests an ineligible refund.
- System explains the applicable policy.
- No payment action occurs.
- Case is escalated if appropriate.

### Demo C: Failure recovery

- Provider accepts a refund but the response times out.
- Application restarts.
- Workflow reconciles the existing operation.
- No second refund is created.

A simple interface needs only:

- Customer complaint panel.
- Workflow timeline.
- Evidence and policy panel.
- Approval queue.
- Final outcome panel.

Display structured events and concise explanations rather than private model reasoning.

---

## 19. Write a strong README and résumé entry

Your README should explain:

1. The customer problem and supported scope.
2. Architecture and workflow.
3. Setup and seed-data instructions.
4. Tool contracts and approval design.
5. Refund idempotency and recovery behavior.
6. Evaluation methodology and measured results.
7. Known limitations.
8. Demo link.

**Résumé template — replace placeholders only after measuring:**

> Built a customer support resolution system using Python, LangGraph, FastAPI, and PostgreSQL, integrating policy retrieval, customer APIs, human-approved refunds, and auditable ticket updates.
> 

> Achieved [X%] verified resolution on [N] held-out synthetic cases; tested duplicate-refund prevention and recovery across [M] retry, concurrency, and crash scenarios.
> 

Do not describe synthetic test results as production performance.

---

## 20. Final project checklist

Before sharing the project, confirm:

- [ ]  Customer access is derived from authenticated identity.
- [ ]  Order ownership is checked server-side.
- [ ]  Policies have versions and citations.
- [ ]  Model outputs use validated schemas.
- [ ]  Financial eligibility is enforced in code.
- [ ]  Refunds require approval bound to an exact proposal.
- [ ]  Expired or changed proposals require fresh approval.
- [ ]  Retries and concurrent requests cannot duplicate refunds.
- [ ]  Unknown payment outcomes trigger reconciliation.
- [ ]  Ticket updates reflect verified external status.
- [ ]  Workflow state survives a restart.
- [ ]  Evaluation checks business state, not just response text.
- [ ]  Results distinguish automated and human-assisted resolution.
- [ ]  The README documents scope, tradeoffs, and limitations.

**Stack:** Python, LangGraph, FastAPI, PostgreSQL

**Project level:** Intermediate

**Suggested timeline:** 3–4 weeks, depending on your experience

**What you’ll build:** A support system that investigates customer problems, retrieves relevant policies, proposes solutions, executes approved actions, and records the outcome.

---