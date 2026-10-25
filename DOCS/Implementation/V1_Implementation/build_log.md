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

