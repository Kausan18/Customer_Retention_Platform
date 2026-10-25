# Customer Support Agent — Build Log

---

## Session 1 — Week 1, Day 1–2: Project Setup, Schema, and Seed Data

**Date:** 2026-10-24
**Status:** ✅ Checkpoint 1 passed

---

### Goal

Establish the data foundation that every other V1 component depends on:
- A clean V1 folder structure
- The full SQLite schema for all V1 tables
- Synthetic orders, shipments, payments, and returns layered on top of real customer IDs from the CSV
- A verified, reproducible database state before any LangGraph or FastAPI code is written

---

### Context

The project started from a working base-version terminal chatbot (`app.py`, `injection.py`, `test_injection.py`) that:
- Imported `customer_shopping_data.csv` (99,457 rows) into SQLite
- Authenticated users with a shared demo PIN
- Answered per-customer purchase-summary questions via Gemini
- Had input screening, rate limiting, and audit logging

That base version was flat-structured and single-purpose. V1 extends it into a full agentic support system with 5 workflows, 5 guardrail types, RAG, and human-in-the-loop approvals. Before any of that could be built, the folder structure and data layer had to be right.

---

### Step 1 — Folder Restructure

**What:** Moved the flat base-version layout into the V1 directory tree defined in `V1_Project_Definition.md`.

**Why:** Every V1 module (`app/tools/`, `app/agents/`, `app/guardrails/`, etc.) depends on the package structure being in place before it can be written. Restructuring now, while the codebase is small, costs nothing. Doing it later would break imports across dozens of files.

**How:** Ran a PowerShell script from the project root that:
1. Backed up the current project directory
2. Created all V1 directories and `__init__.py` package markers
3. Moved `app.py` → `app/main.py`, `injection.py` → `app/guardrails/input_screener.py`, `test_injection.py` → `tests/test_guardrails.py`
4. Created empty placeholder files for every V1 module

**Issue encountered:** After the move, running `python -m app.main` printed the old API-key error. Cause: `load_environment()` resolved `.env` relative to the old flat path. Fix: updated the path to `Path(__file__).resolve().parent.parent / ".env"` so it resolves correctly from `app/main.py` to the project root.

**Outcome:** V1 folder structure in place. Base-version Gemini chatbot still runs correctly from the new location.

**Final structure (relevant parts):**
```
customer-support-agent/
├── app/
│   ├── api/                    ← tickets.py, runs.py, approvals.py (empty)
│   ├── auth/                   ← session.py (empty)
│   ├── schemas/                ← ticket, proposal, approval, workflow_state (empty)
│   ├── agents/                 ← router + 5 workflow graphs (empty)
│   ├── tools/                  ← order, catalog, payment, shipping, policy, ticket tools (empty)
│   ├── guardrails/             ← input_screener.py (moved from injection.py), + 4 more (empty)
│   ├── services/               ← eligibility.py, audit.py (empty)
│   ├── db/
│   │   ├── seed.py             ← ✅ written this session
│   │   └── database.py         ← ✅ written this session
│   └── main.py                 ← moved from app.py, still runs base chatbot
├── mock_services/              ← payment_service.py, shipping_service.py (empty)
├── rag/                        ← embedder.py, retriever.py, ingest_policies.py (empty)
├── policies/                   ← 5 policy .md files (empty)
├── evals/                      ← eval_cases.json, run_evals.py, reports/ (empty)
├── tests/                      ← test_guardrails.py (moved from test_injection.py), + 3 more (empty)
├── streamlit_app.py            ← (empty)
├── data/
│   ├── customer_shopping_data.csv   ← source CSV, never modified
│   └── customer_support.sqlite3     ← ✅ created this session
├── verify_seed.py              ← ✅ written this session
├── BUILD_LOG.md                ← this file
├── .env
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

---

### Step 2 — `app/db/seed.py`

**What:** Script that creates the full V1 schema and populates it with reproducible synthetic data.

**Why:** Tools like `get_order()`, `get_payment_status()`, and `get_return_status()` need real rows to query. Eligibility logic needs delivered orders, returns with passed/failed inspections, and payments with varying refunded amounts. Eval cases need stable, predictable IDs. All of that requires seeded data before a single line of LangGraph is written.

**How:**

1. **Customer IDs** — read distinct `customer_id` values from `customer_shopping_data.csv` (read-only). These become the owner pool for synthetic orders. No CSV data is modified or overwritten.

2. **Schema creation** — drops and recreates the 11 V1 tables in reverse dependency order (children first, then parents), then recreates them. The existing `purchases` table managed by the base version is never touched.

   Tables created:
   | Table | Purpose |
   |---|---|
   | `product_catalog` | 30 products across 6 categories |
   | `orders` | 200 synthetic orders with status and timestamps |
   | `order_items` | 1–3 items per order, linked to catalog |
   | `shipments` | One per order, status tracks order lifecycle |
   | `payments` | One per order, tracks paid/refunded amounts in paise |
   | `returns` | ~30% of delivered orders, with inspection outcomes |
   | `tickets` | Support tickets (written by workflows later) |
   | `workflow_runs` | LangGraph run tracking (written by workflows later) |
   | `action_proposals` | Refund/exchange proposals with SHA256 tamper hash |
   | `approvals` | Reviewer decisions bound to proposals |
   | `audit_events` | Immutable event log for every agent action |

3. **Product catalog** — 30 hardcoded products across Electronics, Clothing, Cosmetics, Food, Shoes, Toys. Prices in paise (1 INR = 100 paise). All 6 categories covered, matching the CSV categories.

4. **Order generation** — `random.Random(42)` (fixed seed) samples 200 customer IDs and generates one order each. Status is weighted: 60% delivered, 15% shipped, 10% confirmed, 10% pending, 5% cancelled. Each order gets 1–3 randomly selected products, total computed from item prices.

5. **Shipment generation** — one per order. Status derived from order status (e.g. delivered order → delivered shipment, shipped order → in_transit/out_for_delivery/delayed).

6. **Payment generation** — one per order. Paid orders (delivered, shipped, confirmed) get `status = paid`. Cancelled orders get `status = failed`. `refunded_minor` starts at 0 for all — the refund workflow updates this later.

7. **Return generation** — for 30% of delivered orders: `received_at` is 2–20 days after delivery. Inspection outcomes are weighted 70% passed / 30% failed to give a realistic mix of eligible and ineligible refund cases. Final statuses include `inspecting` and `received` to simulate in-progress returns.

8. **Atomicity** — entire generation runs in one transaction. Any error triggers a full rollback, so the database is never left in a partial state.

**Key design decisions:**
- Money stored in integer paise throughout (e.g. ₹2,999 = 299900). No floats.
- Fixed seed `42` makes the dataset reproducible across machines. Eval cases referencing specific order IDs (`ord_0006`, etc.) will always find the same records.
- `action_proposals.payload_hash` (SHA256) is included in the schema now even though the tool authorizer writes to it in Week 1 Day 5–6. Getting the schema right once is cheaper than a migration.
- `approvals.expires_at` same reasoning — the column is there so approval expiry can be tested without altering the schema later.

**Run command:**
```powershell
python -m app.db.seed
```

---

### Step 3 — `app/db/database.py`

**What:** Thin SQLite connection helper shared by all tool modules.

**Why:** Without this, each tool (`order_tools.py`, `payment_tools.py`, etc.) would open its own `sqlite3.connect()` call and independently configure `row_factory`, foreign keys, and the database path. That creates drift: one file forgets `foreign_keys = ON`, another uses a different path, a third doesn't commit properly. Centralizing it in one place means every tool gets consistent behavior for free.

**How:** Four public helpers:

| Helper | What it does |
|---|---|
| `get_connection()` | Context manager: opens connection, commits on clean exit, rolls back on exception, always closes |
| `fetch_one(query, params)` | Parameterized SELECT returning first row as dict or None |
| `fetch_all(query, params)` | Parameterized SELECT returning list of dicts |
| `execute(query, params)` | Parameterized INSERT/UPDATE/DELETE with auto-commit |
| `execute_many(statements)` | Multiple statements in one atomic transaction |
| `row_exists(table, column, value)` | Narrow existence check for ownership guards |

Every helper enforces:
- `PRAGMA foreign_keys = ON` on every connection
- `row_factory = sqlite3.Row` so callers get named-column dicts, not tuples
- Parameterized queries only (`?` placeholders) — user values never interpolated into SQL strings

**Rollback test confirmed:** When an exception is raised mid-transaction inside `get_connection()`, the writes do not persist. Verified by inserting a row, raising an error before commit, and confirming the row still existed afterward.

**Run command:** Not run directly. Imported by tool modules starting Week 2.

---

### Step 4 — `verify_seed.py`

**What:** Standalone script that inspects the seeded database and reports counts, sample records, and integrity checks.

**Why:** The V1 plan's Checkpoint 1 criterion is explicit: "verify_seed.py prints row counts and sample records for each table." Having a repeatable, zero-friction verification step means anyone cloning the repo can confirm the seed is working correctly in under 10 seconds.

**How:** Queries each table for row count, pulls one sample record per populated table, runs 6 integrity assertions:
- Order items with missing product references (expect 0)
- Order items with missing order references (expect 0)
- Orders where `total_minor` doesn't match sum of item prices (expect 0)
- Orders with no shipment (expect 0)
- Orders with no payment (expect 0)
- Returns on orders that aren't delivered (expect 0)

**Run command:**
```powershell
python verify_seed.py
```

---

### Checkpoint 1 — Verified Output

```
=== Row counts ===
  product_catalog      30
  orders               200
  order_items          417
  shipments            200
  payments             200
  returns              27
  tickets              0
  workflow_runs        0
  action_proposals     0
  approvals            0
  audit_events         0

=== Order status distribution ===
  delivered    125
  shipped       31
  pending       17
  confirmed     17
  cancelled     10

=== Integrity checks ===
  Order items with missing product:  0 (expect 0)
  Order items with missing order:    0 (expect 0)
  Orders with total/item mismatch:   0 (expect 0)
  Orders with no shipment:           0 (expect 0)
  Orders with no payment:            0 (expect 0)
  Returns on undelivered orders:     0 (expect 0)
```

All checks passed. Output matches exactly on both the build machine and the developer's local machine.

---

### Notes for Next Session

- `ord_0006` is a delivered order with a passed-inspection return — the canonical "eligible refund" case for happy-path testing and Demo A.
- 125 delivered orders give enough volume for both eligible (return received + passed) and ineligible (no return, or failed inspection, or outside 30-day window) refund eval cases.
- The `purchases` table from the base version still exists in the same database file alongside the new V1 tables. The base-version chatbot still runs correctly.
- Empty placeholder files exist for every V1 module. Nothing imports `database.py` yet — that starts in Week 2 when tools are built.

---

---

---

## Session 2 — Week 1, Day 3–4: Mock Services and Eligibility Logic

**Date:** 2026-10-25
**Status:** ✅ Checkpoint 2 passed

---

### Goal

Build the three files that sit on either side of the eligibility decision:

- `mock_services/payment_service.py` — simulates a third-party payment provider with idempotency and configurable failure modes
- `mock_services/shipping_service.py` — simulates a shipping carrier with configurable failure modes
- `app/services/eligibility.py` — pure Python deterministic refund and exchange eligibility rules, no LLM

These three files are independent of each other and independent of LangGraph. Checkpoint 2 proves the business logic is correct before any agent touches it.

---

### Context

Session 1 left a verified SQLite database with 200 orders, 30 products, 27 returns, and all integrity checks passing. Session 2 builds the layer immediately above that data: the services that read it (eligibility) and the external mocks that write to it (payment, shipping). No FastAPI, no LangGraph, no LLM is involved in this session.

---

### Step 1 — `mock_services/payment_service.py`

**What:** Self-contained module simulating a payment provider. Exposes three operations: `charge`, `refund`, and `get_status`. Reads and writes the `payments` table directly via `app/db/database.py`.

**Why:** The refund workflow tool layer (Week 1 Day 5–6) calls this service. Its contract — what it returns on success, failure, and timeout — must be defined before the tools that consume it are written. More critically, `refund()` is the only operation in V1 where a duplicate write causes direct, unrecoverable financial damage. That makes idempotency a first-class concern here and nowhere else in V1.

**How:**

`charge(order_id, amount_minor, payment_method)` — marks an order as paid. Inserts or updates the `payments` row. Returns `{"success": True, "transaction_id": "txn_..."}` or a structured failure.

`refund(order_id, amount_minor, idempotency_key)` — the critical operation. Before any DB read, failure mode check, or balance validation, checks a module-level `_idempotency_store` dict keyed on `idempotency_key`. If the key exists and the amount matches, returns the stored result immediately with no DB write. If the key exists but the amount differs, returns `IDEMPOTENCY_PAYLOAD_MISMATCH` — this prevents a race condition where a bug changes the amount between retries. Only after a confirmed successful DB write does the result get stored in `_idempotency_store`. Failed operations (provider declined, timeout, pending) are never stored, so retries after failure are safe.

Balance check: rejects if `amount_minor > paid_amount_minor - refunded_minor`. Updates `payments.refunded_minor` and sets status to `refunded` or `partially_refunded` depending on whether the balance is fully consumed.

`get_status(order_id)` — reads current payment state. Returns structured dict with `status`, `amount_minor`, `refunded_minor`, `payment_method`, `paid_at`.

**Failure modes** — controlled by `set_mode(mode)`:

| Mode | Behavior |
|---|---|
| `success` (default) | Operations complete normally |
| `failure` | `refund()` returns `PROVIDER_DECLINED, retryable=False` |
| `timeout` | `refund()` raises `TimeoutError` after short sleep |
| `delay` | `refund()` sleeps 2.5 seconds then proceeds normally |
| `pending` | `refund()` returns `PROCESSING, retryable=True, status=pending` |

`reset_idempotency_store()` is provided as a test helper to clear stored results between test cases.

**Key design decisions:**
- Idempotency store checked first, before failure modes. A confirmed prior result is always returned as-is regardless of current mode.
- Failed calls are not stored — only successful committed writes are. This matches real provider contracts.
- Structured error returns throughout (`{"success": False, "error_code": "...", "retryable": bool, "message": "..."}`). No bare exceptions for expected failure paths.
- Service writes directly to `payments` table. It has no separate DB — `payments` is the financial ledger.

---

### Step 2 — `mock_services/shipping_service.py`

**What:** Simulates a shipping carrier. Exposes `create_shipment` and `get_tracking`. Reads and writes the `shipments` table via `database.py`.

**Why:** Needed by the `order_tracking_graph` (Week 2) which translates raw shipment status into customer-friendly language. The service must exist and return realistic status values before that LLM node is written.

**How:**

`create_shipment(order_id, carrier, items_summary)` — inserts a shipment row with `status=processing`. Rejects if a shipment already exists for the order (`SHIPMENT_ALREADY_EXISTS`). Assigns carrier from argument or deterministically from `_CARRIERS` list based on `hash(order_id)`.

`get_tracking(order_id)` — reads the shipment row and returns carrier, tracking ID, status, estimated delivery, and last updated. Status values match what seed.py seeded: `processing`, `in_transit`, `out_for_delivery`, `delivered`, `delayed`.

**Failure modes** — `set_mode()` supports `success`, `failure` (`CARRIER_UNAVAILABLE, retryable=True`), `timeout`.

**Why simpler than payment service:** No financial consequence. A wrong tracking status doesn't move money. No idempotency store needed — `create_shipment` is naturally idempotent via the existence check.

---

### Step 3 — `app/services/eligibility.py`

**What:** Pure Python module with two public functions: `check_refund_eligibility(order_id)` and `check_exchange_eligibility(order_id)`. No LLM. No tools. SQL reads via `database.py` and business rule evaluation only.

**Why pure code:** V1_Project_Definition.md is explicit: `check_eligibility <- CODE: deterministic rules, not LLM`. Financial eligibility must be reproducible, auditable, and immune to hallucination. An LLM could decide a 45-day-old order is eligible because the customer seems upset. This code cannot.

**`check_refund_eligibility(order_id)` — 8 conditions checked in order, first failure short-circuits:**

| Rule | Condition | Error code on failure |
|---|---|---|
| 1 | Order exists | `ORDER_NOT_FOUND` |
| 2 | `orders.status == 'delivered'` | `NOT_DELIVERED` |
| 3 | Within 30 days of `delivered_at` | `OUTSIDE_WINDOW` |
| 4 | `payments.status` in `{paid, partially_refunded}` | `PAYMENT_NOT_PAID` |
| 5 | `paid_amount_minor - refunded_minor > 0` | `ALREADY_REFUNDED` |
| 6 | A return row exists for this order | `NO_RETURN_FOUND` |
| 7 | `returns.status` in `{received, inspecting, passed}` | `RETURN_NOT_RECEIVED` |
| 8 | `returns.inspection_status == 'passed'` | `INSPECTION_NOT_PASSED` |

On pass: `{"eligible": True, "order_id": ..., "refundable_amount_minor": ..., "reason": "..."}`.
On fail: `{"eligible": False, "order_id": ..., "error_code": "...", "reason": "..."}`.

**`check_exchange_eligibility(order_id)` — 4 conditions:**

| Rule | Condition | Error code on failure |
|---|---|---|
| 1 | Order exists | `ORDER_NOT_FOUND` |
| 2 | `orders.status == 'delivered'` | `NOT_DELIVERED` |
| 3 | Within 15 days of `delivered_at` (tighter than refund) | `OUTSIDE_WINDOW` |
| 4 | `payments.status == 'paid'` (not partially/fully refunded) | `PAYMENT_NOT_PAID` / `ALREADY_REFUNDED` |

On pass: `{"eligible": True, "order_id": ..., "items": [...], "reason": "..."}` — includes order items with `product_id`, `product_name`, `category`, `quantity`, `price_minor` for the exchange workflow to find a same-category replacement.

**Key design decisions:**
- Both functions accept an optional `now: datetime` parameter for testability — pass a fixed datetime to test window boundary conditions without needing aged seed data.
- `_parse_dt()` tries four formats including `%Y-%m-%dT%H:%M:%SZ` (Z-suffix) because SQLite timestamps from the seed include the Z.
- Returns structured dicts, not booleans or exceptions. The calling workflow node reads `result["eligible"]` and `result["error_code"]` to branch. Raising exceptions for "business rule not met" would conflate expected outcomes with unexpected errors.
- Uses `fetch_all` for returns and takes the most recent row (`ORDER BY id DESC LIMIT 1`) to handle the case where multiple return attempts exist.

---

### Step 4 — `tests/test_eligibility.py`

**What:** Checkpoint 2 verification script. Queries the seeded DB to find real rows matching each case shape, calls the eligibility functions, asserts expected verdicts.

**Why query-based not ID-hardcoded:** Hardcoding `ord_0006` as the canonical eligible case (as noted in Session 1) proved fragile — after the seed date fix, `ord_0006` landed on `pending` status. The test now finds the first order satisfying each shape dynamically, making it resilient to re-seeding.

**13 cases across two groups:**

Refund (7): anchor (first eligible order), eligible fields present, deterministic repeat, not-delivered block, no-return block, outside-window block.
Exchange (6): anchor (first eligible order), eligible with items returned, deterministic repeat, items include product fields, not-delivered block, outside-window block.

---

### Issues Encountered and Resolutions

**Issue 1: `delivered_at` timestamps had `Z` suffix**
The seed stored timestamps as `'2026-10-20T20:10:55Z'`. `_parse_dt` in `eligibility.py` only tried formats without the `Z`, so `datetime.strptime` raised `ValueError` on every date parse, causing the window check to never run. Fix: added `"%Y-%m-%dT%H:%M:%SZ"` as the first format tried in `_DATE_FORMATS`.

**Issue 2: Seed generated August dates regardless of when it ran**
`delivered_at` was computed as `created_at + timedelta(days=rng.randint(1, 10))` where `created_at` was itself offset from a base date in the past. With fixed seed 42, every run produced the same August timestamps — all outside the 30-day refund window. Fix: changed to `delivered_at = now - timedelta(days=rng.randint(1, 10))` so delivered orders are always 1–10 days old relative to seed time, and added a 20% bucket of `rng.randint(35, 60)` days for outside-window test coverage.

**Issue 3: `find_eligible_refund_order()` query not matching eligible rows**
The SQL JOIN was correct but the in-Python window check was missing — the query returned orders without filtering by `delivered_at` age, so it picked orders that passed the DB JOIN conditions but were still outside the 30-day window at runtime. Fix: added a `_parse_dt_test()` helper in the test file and a Python-side window filter that iterates results and returns the first one within the applicable window.

**Issue 4: Stale SQLite files causing path confusion**
Three extra SQLite files existed across `data/` and `app/data/`: `customer_shopping.sqlite3`, `customer-shopping.sqlite3`, and a stale `customer_support.sqlite3` in `app/data/`. These were remnants from the base version and the Session 1 restructure. All three were deleted. The only SQLite file in the project is now `data/customer_support.sqlite3`.

---

### Checkpoint 2 — Verified Output

```
=== Checkpoint 2 Results ===
  PASS | RF-ANCHOR first eligible refund order (ord_0167)  | eligible=True reason=All refund conditions met: delivered within window, payment paid, return received, inspection passed.
  PASS | RF-01 eligible (ord_0167)                         | eligible=True refundable=1409500
  PASS | RF-02 deterministic repeat (ord_0167)             | Calling check_refund_eligibility twice gives the same verdict
  PASS | RF-03 fields present (ord_0167)                   | order_status=delivered
  PASS | RF-04 not delivered (ord_0001)                    | error_code=NOT_DELIVERED
  PASS | RF-05 no return found (ord_0009)                  | error_code=NO_RETURN_FOUND
  PASS | RF-06 outside window (ord_0119)                   | error_code=OUTSIDE_WINDOW
  PASS | EX-ANCHOR first eligible exchange order (ord_0009)| eligible=True reason=All exchange conditions met: delivered within window, payment paid.
  PASS | EX-01 eligible (ord_0009)                         | eligible=True items=3
  PASS | EX-02 deterministic repeat (ord_0009)             | Calling check_exchange_eligibility twice gives the same verdict
  PASS | EX-03 items include product fields (ord_0009)     | Each returned item has product_id and category for same-category lookup
  PASS | EX-04 not delivered (ord_0001)                    | error_code=NOT_DELIVERED
  PASS | EX-05 outside 15-day window (ord_0119)            | error_code=OUTSIDE_WINDOW

13/13 cases passed.
```

---

### Notes for Next Session

- `ord_0167` is the new canonical eligible refund case (delivered recently, return received, inspection passed, ₹14,095 refundable).
- `ord_0009` is the canonical eligible exchange case (delivered recently, paid, 3 items available for same-category lookup).
- `ord_0119` is the canonical outside-window case for both refund and exchange boundary tests.
- The idempotency store in `payment_service.py` is process-scoped. `reset_idempotency_store()` must be called between test cases that test retry behavior — noted for the tool authorizer tests in Week 1 Day 5–6.
- `mock_services/` has no `__init__.py` yet — add one before importing from FastAPI or tool modules.

---

### Up Next — Week 1, Day 5–6

- `app/api/tickets.py`, `app/api/runs.py`, `app/api/approvals.py` — all 7 FastAPI endpoints
- `app/guardrails/intent_validator.py`, `app/guardrails/output_validator.py`, `app/guardrails/tool_authorizer.py`, `app/guardrails/response_grounder.py` — remaining 4 guardrail types
- `app/auth/session.py` — extend base-version session auth for V1
- `tests/test_guardrails.py` — all 5 guardrail types, at least 2 cases each