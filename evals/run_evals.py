
"""
V1 eval harness. Hits the live FastAPI server over HTTP — exercises auth,
guardrails, LangGraph workflows, tools, and the real SQLite DB exactly as a
real caller would. Does not call graph functions directly.

Usage:
    1. Start the server:  uvicorn app.main:app --reload
    2. In a second terminal, from the project root:
       python evals/run_evals.py
"""

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))  # so we can import app.services.eligibility

BASE_URL = "http://127.0.0.1:8000"
DEMO_PIN = "1234"
REVIEWER_PIN = "9999"
DB_PATH = PROJECT_ROOT / "data" / "customer_support.sqlite3"
CASES_PATH = Path(__file__).resolve().parent / "eval_cases.json"
REPORT_PATH = Path(__file__).resolve().parent / "reports" / "v1_results.json"

POLL_ATTEMPTS = 10
POLL_DELAY_SECONDS = 1.0

from app.services import eligibility  # noqa: E402


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def run_query(conn, query):
    cur = conn.execute(query)
    return cur.fetchall()


def substitute(template, ctx):
    if template is None:
        return None
    out = template
    for key, val in ctx.items():
        out = out.replace("{" + key + "}", str(val))
    return out


# ---------------------------------------------------------------------------
# Strategy resolvers — each returns a dict of context values or None if no
# matching row exists (re-seed safe: queried fresh on every run)
# ---------------------------------------------------------------------------

def strategy_any_customer(conn):
    row = conn.execute("SELECT customer_id FROM orders ORDER BY id LIMIT 1").fetchone()
    return {"customer_id": row["customer_id"]} if row else None


def strategy_any_delivered(conn):
    row = conn.execute(
        "SELECT customer_id, id AS order_id FROM orders WHERE status='delivered' ORDER BY id LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def strategy_not_delivered_order(conn):
    row = conn.execute(
        "SELECT customer_id, id AS order_id FROM orders WHERE status != 'delivered' ORDER BY id LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def strategy_mismatched_customer_order(conn):
    order_row = conn.execute("SELECT customer_id, id AS order_id FROM orders ORDER BY id LIMIT 1").fetchone()
    if not order_row:
        return None
    other = conn.execute(
        "SELECT DISTINCT customer_id FROM orders WHERE customer_id != ? LIMIT 1",
        (order_row["customer_id"],),
    ).fetchone()
    if not other:
        return None
    return {"customer_id": other["customer_id"], "order_id": order_row["order_id"]}


def strategy_customer_without_shoes_order(conn):
    row = conn.execute(
        """
        SELECT DISTINCT o.customer_id FROM orders o
        WHERE NOT EXISTS (
            SELECT 1 FROM order_items oi
            WHERE oi.order_id = o.id AND oi.category = 'Shoes'
        )
        LIMIT 1
        """
    ).fetchone()
    return {"customer_id": row["customer_id"]} if row else None


def _first_eligible(conn, table_status_filter, check_fn, wanted_error_code=None):
    rows = conn.execute(
        f"SELECT id AS order_id, customer_id FROM orders WHERE status='{table_status_filter}' ORDER BY id"
    ).fetchall()
    for row in rows:
        result = check_fn(row["order_id"])
        if wanted_error_code is None and result.get("eligible"):
            return {"order_id": row["order_id"], "customer_id": row["customer_id"]}
        if wanted_error_code is not None and result.get("error_code") == wanted_error_code:
            return {"order_id": row["order_id"], "customer_id": row["customer_id"]}
    return None


def strategy_eligible_refund(conn):
    return _first_eligible(conn, "delivered", eligibility.check_refund_eligibility)


def strategy_second_eligible_refund(conn):
    """Returns the second eligible refund order — distinct from the first,
    so ER-04 doesn't reuse the same order as ER-01 (which executes a real refund)."""
    rows = conn.execute(
        "SELECT id AS order_id, customer_id FROM orders WHERE status='delivered' ORDER BY id"
    ).fetchall()
    found = 0
    for row in rows:
        result = eligibility.check_refund_eligibility(row["order_id"])
        if result.get("eligible"):
            found += 1
            if found == 2:
                return {"order_id": row["order_id"], "customer_id": row["customer_id"]}
    return None


def strategy_eligible_exchange(conn):
    return _first_eligible(conn, "delivered", eligibility.check_exchange_eligibility)


def strategy_second_eligible_exchange(conn):
    """Returns the second eligible exchange order — distinct from the first,
    so EX-05 doesn't reuse the same order as EX-01."""
    rows = conn.execute(
        "SELECT id AS order_id, customer_id FROM orders WHERE status='delivered' ORDER BY id"
    ).fetchall()
    found = 0
    for row in rows:
        result = eligibility.check_exchange_eligibility(row["order_id"])
        if result.get("eligible"):
            found += 1
            if found == 2:
                return {"order_id": row["order_id"], "customer_id": row["customer_id"]}
    return None


def strategy_ineligible_refund_outside_window(conn):
    return _first_eligible(conn, "delivered", eligibility.check_refund_eligibility, "OUTSIDE_WINDOW")


def strategy_ineligible_exchange_outside_window(conn):
    return _first_eligible(conn, "delivered", eligibility.check_exchange_eligibility, "OUTSIDE_WINDOW")


def product_strategy_first_in_stock_product(conn):
    row = conn.execute(
        "SELECT id AS product_id, name AS product_name FROM product_catalog WHERE in_stock=1 ORDER BY id LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


CUSTOMER_STRATEGIES = {
    "any_customer": strategy_any_customer,
    "any_delivered": strategy_any_delivered,
    "not_delivered_order": strategy_not_delivered_order,
    "mismatched_customer_order": strategy_mismatched_customer_order,
    "customer_without_shoes_order": strategy_customer_without_shoes_order,
    "eligible_refund": strategy_eligible_refund,
    "second_eligible_refund": strategy_second_eligible_refund,
    "eligible_exchange": strategy_eligible_exchange,
    "second_eligible_exchange": strategy_second_eligible_exchange,
    "ineligible_refund_outside_window": strategy_ineligible_refund_outside_window,
    "ineligible_exchange_outside_window": strategy_ineligible_exchange_outside_window,
}

PRODUCT_STRATEGIES = {
    "first_in_stock_product": product_strategy_first_in_stock_product,
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, step, response):
        self.step = step
        self.response = response
        super().__init__(f"{step} -> HTTP {response.status_code}: {response.text[:300]}")


def login(customer_id):
    r = requests.post(f"{BASE_URL}/auth/login", json={"customer_id": customer_id, "pin": DEMO_PIN})
    return r


def logout(token):
    try:
        requests.post(f"{BASE_URL}/auth/logout", headers={"Authorization": f"Bearer {token}"}, timeout=3)
    except requests.exceptions.RequestException:
        pass  # best-effort; never fail a case because logout errored


def create_ticket(token, category, message, order_id=None):
    body = {"category": category, "message": message}
    if order_id:
        body["order_id"] = order_id
    r = requests.post(
        f"{BASE_URL}/tickets", json=body, headers={"Authorization": f"Bearer {token}"}
    )
    return r


def start_run(token, ticket_id):
    r = requests.post(
        f"{BASE_URL}/tickets/{ticket_id}/runs", headers={"Authorization": f"Bearer {token}"}
    )
    return r


def send_message(token, run_id, text):
    r = requests.post(
        f"{BASE_URL}/runs/{run_id}/messages",
        json={"message": text},
        headers={"Authorization": f"Bearer {token}"},
    )
    return r


def get_run(token, run_id):
    r = requests.get(f"{BASE_URL}/runs/{run_id}", headers={"Authorization": f"Bearer {token}"})
    return r


def get_approvals():
    r = requests.get(f"{BASE_URL}/approvals", headers={"X-Reviewer-Pin": REVIEWER_PIN})
    return r


def decide_approval(approval_id, decision):
    r = requests.post(
        f"{BASE_URL}/approvals/{approval_id}/decision",
        json={"decision": decision, "reviewer_id": "eval_harness"},
        headers={"X-Reviewer-Pin": REVIEWER_PIN},
    )
    return r


def poll_run(token, run_id, treat_paused_as_terminal=False):
    for _ in range(POLL_ATTEMPTS):
        r = get_run(token, run_id)
        if r.status_code == 200:
            status = r.json().get("status")
            terminal = {"completed", "failed"}
            if treat_paused_as_terminal:
                terminal.add("paused")
            if status in terminal:
                return r.json(), False
        time.sleep(POLL_DELAY_SECONDS)
    r = get_run(token, run_id)
    body = r.json() if r.status_code == 200 else {}
    return body, True  # timed out


def force_expire_approval(approval_id):
    conn = get_conn()
    conn.execute(
        "UPDATE action_proposals SET expires_at = ? WHERE id = (SELECT proposal_id FROM approvals WHERE id = ?)",
        (datetime.now(timezone.utc).isoformat(), approval_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Case runner
# ---------------------------------------------------------------------------

def run_case(case, conn):
    case_id = case["id"]
    if case.get("skip"):
        return {"id": case_id, "result": "SKIP", "reason": case.get("note", "marked skip")}

    setup = case["setup"]
    ctx = {"case_start_time": datetime.now(timezone.utc).isoformat()}

    # Resolve customer strategy
    strat_name = setup.get("customer_strategy")
    if strat_name:
        resolved = CUSTOMER_STRATEGIES[strat_name](conn)
        if not resolved:
            return {"id": case_id, "result": "SKIP", "reason": f"no row matched strategy '{strat_name}'"}
        ctx.update(resolved)

    # Resolve product strategy if present
    prod_strat_name = setup.get("product_strategy")
    if prod_strat_name:
        prod = PRODUCT_STRATEGIES[prod_strat_name](conn)
        if not prod:
            return {"id": case_id, "result": "SKIP", "reason": f"no row matched product strategy '{prod_strat_name}'"}
        ctx.update(prod)

    message = setup.get("message") or substitute(setup.get("message_template"), ctx)
    category = setup["category"]
    customer_id = ctx.get("customer_id")

    blocked_detected = False
    final_run = {}
    http_log = []

    # --- special case: rate limit burst (GR-10) ---
    # Handled outside the normal login/logout flow — uses its own dedicated customer.
    if case.get("special") == "rate_limit_burst":
        try:
            burst_conn = get_conn()
            burst_row = burst_conn.execute(
                "SELECT DISTINCT customer_id FROM orders WHERE customer_id != ? ORDER BY customer_id DESC LIMIT 1",
                (customer_id,),
            ).fetchone()
            burst_conn.close()
            if not burst_row:
                return {"id": case_id, "result": "SKIP", "reason": "no second customer found for burst test"}
            burst_customer_id = burst_row["customer_id"]
            login_r = login(burst_customer_id)
            if login_r.status_code != 200:
                return {"id": case_id, "result": "FAIL", "reason": "login failed for burst test"}
            token = login_r.json()["token"]
            burst_results = []
            for _ in range(case.get("burst_count", 9)):
                tr = create_ticket(token, category, message)
                burst_results.append(tr.status_code)
            logout(token)
            ninth_blocked = burst_results[-1] >= 400
            earlier_ok = all(s < 400 for s in burst_results[:-1])
            if ninth_blocked and earlier_ok:
                return {"id": case_id, "result": "PASS", "reason": "9th request rate-limited, earlier 8 succeeded"}
            return {
                "id": case_id,
                "result": "FAIL",
                "reason": f"burst status codes: {burst_results} (expected last blocked, rest ok)",
            }
        except requests.exceptions.ConnectionError:
            print("\n[FATAL] Could not reach the server. Is `uvicorn app.main:app` running?")
            sys.exit(1)

    # --- normal flow ---
    token = None  # initialised before try so finally can always call logout safely
    try:
        login_r = login(customer_id)
        http_log.append(("login", login_r.status_code))
        if login_r.status_code != 200:
            return {"id": case_id, "result": "FAIL", "reason": f"login failed: {login_r.status_code}"}
        token = login_r.json()["token"]

        ticket_r = create_ticket(token, category, message, order_id=ctx.get("order_id"))
        http_log.append(("create_ticket", ticket_r.status_code))
        if ticket_r.status_code != 201:
            blocked_detected = True
        else:
            ticket_id = ticket_r.json()["ticket_id"]
            ctx["ticket_id"] = ticket_id  # available for forbidden_db_checks substitution

            run_r = start_run(token, ticket_id)
            http_log.append(("start_run", run_r.status_code))
            if run_r.status_code != 201:
                blocked_detected = True
            else:
                run_body = run_r.json()
                run_id = run_body["run_id"]
                ctx["run_id"] = run_id  # available for forbidden_db_checks substitution

                # Run failed immediately after start — surface the actual error.
                if run_body.get("status") == "failed":
                    failed_run, _ = poll_run(token, run_id)
                    final_response = (
                        failed_run.get("final_response")
                        or run_body.get("final_response")
                        or "No final response was returned."
                    )
                    error_event = conn.execute(
                        """
                        SELECT payload FROM audit_events
                        WHERE run_id = ? AND event_type = 'workflow_error'
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (run_id,),
                    ).fetchone()
                    error_details = ""
                    if error_event and error_event["payload"]:
                        try:
                            payload = json.loads(error_event["payload"])
                            if isinstance(payload, str):
                                payload = json.loads(payload)
                            if isinstance(payload, dict):
                                error_details = (
                                    payload.get("error_message")
                                    or payload.get("error")
                                    or payload.get("error_code")
                                    or ""
                                )
                        except (json.JSONDecodeError, TypeError):
                            error_details = str(error_event["payload"])
                    return {
                        "id": case_id,
                        "result": "FAIL",
                        "reason": (
                            "run failed immediately after start | "
                            f"error={error_details[:200]} | "
                            f"final_response={final_response[:200]} | http_log={http_log}"
                        ),
                    }

                resume = case.get("resume", {"type": "none"})

                if resume["type"] == "customer_message":
                    current_status = run_body.get("status")
                    if current_status not in ("completed", "failed"):
                        msg_r = send_message(token, run_id, resume["value"])
                        http_log.append(("send_message", msg_r.status_code))
                        if msg_r.status_code != 200:
                            blocked_detected = True

                elif resume["type"] in ("approval", "expired_approval"):
                    approval_id = None
                    apr_r = None
                    for _attempt in range(8):
                        time.sleep(0.5)
                        apr_r = get_approvals()
                        if _attempt == 0:
                            http_log.append(("get_approvals", apr_r.status_code))
                        if apr_r.status_code == 200:
                            for a in apr_r.json():
                                a_run_id = a.get("run_id")
                                ticket = a.get("ticket")
                                if isinstance(ticket, dict):
                                    a_ticket_id = ticket.get("ticket_id") or ticket.get("id")
                                else:
                                    a_ticket_id = a.get("ticket_id")
                                if a_run_id == run_id or a_ticket_id == ticket_id:
                                    approval_id = a.get("approval_id") or a.get("id")
                                    break
                            if approval_id:
                                break

                    if not approval_id and apr_r is not None and apr_r.status_code == 200:
                        case_started_at = datetime.fromisoformat(
                            ctx["case_start_time"].replace("Z", "+00:00")
                        )
                        recent_approvals = []
                        for a in apr_r.json():
                            created_at = a.get("created_at") or a.get("proposal_created_at")
                            if not created_at:
                                continue
                            try:
                                created_dt = datetime.fromisoformat(
                                    created_at.replace("Z", "+00:00")
                                )
                            except ValueError:
                                continue
                            if created_dt >= case_started_at:
                                recent_approvals.append((created_dt, a))
                        if recent_approvals:
                            latest = max(recent_approvals, key=lambda item: item[0])[1]
                            approval_id = latest.get("approval_id") or latest.get("id")

                    if not approval_id:
                        blocked_detected = True
                    else:
                        if resume["type"] == "expired_approval":
                            force_expire_approval(approval_id)

                        dec_r = decide_approval(approval_id, resume["value"])
                        http_log.append(("decide_approval", dec_r.status_code))

                        if resume["type"] == "expired_approval":
                            is_expired_rejection = dec_r.status_code == 410
                            result = "PASS" if is_expired_rejection else "FAIL"
                            reason = (
                                "expired approval correctly rejected"
                                if is_expired_rejection
                                else f"expected HTTP 410 for expired approval, got {dec_r.status_code}"
                            )
                            return _with_forbidden_checks(
                                case, ctx, conn, {"id": case_id, "result": result, "reason": reason}
                            )

                treat_paused = resume["type"] == "none"
                final_run, timed_out = poll_run(
                    token, run_id, treat_paused_as_terminal=treat_paused
                )
                if timed_out:
                    return {
                        "id": case_id,
                        "result": "TIMEOUT",
                        "reason": "run did not complete in time",
                        "http_log": http_log,
                    }

    except requests.exceptions.ConnectionError:
        print("\n[FATAL] Could not reach the server. Is `uvicorn app.main:app` running?")
        sys.exit(1)
    finally:
        if token:
            logout(token)

    # --- assertion phase ---
    expect_blocked = case.get("expect_blocked", False)
    if expect_blocked:
        result = "PASS" if blocked_detected else "FAIL"
        reason = "blocked as expected" if blocked_detected else "expected a block but request succeeded"
        reason += f" | http_log={http_log}"
        return _with_forbidden_checks(case, ctx, conn, {"id": case_id, "result": result, "reason": reason})

    if blocked_detected:
        return {"id": case_id, "result": "FAIL", "reason": f"unexpectedly blocked | http_log={http_log}"}

    assertions = case.get("assertions", {})
    failures = []

    if assertions.get("run_status") and final_run.get("status") != assertions["run_status"]:
        failures.append(f"run_status expected {assertions['run_status']}, got {final_run.get('status')}")

    if assertions.get("ticket_status"):
        row = conn.execute(
            "SELECT status FROM tickets WHERE id = ?", (final_run.get("ticket_id"),)
        ).fetchone()
        actual = row["status"] if row else None
        if actual != assertions["ticket_status"]:
            failures.append(f"ticket_status expected {assertions['ticket_status']}, got {actual}")

    response_text = final_run.get("final_response") or ""
    for must in assertions.get("response_must_contain", []):
        if must.lower() not in response_text.lower():
            failures.append(f"response missing required text '{must}'")
    for forbidden in assertions.get("response_must_not_contain", []):
        if forbidden.lower() in response_text.lower():
            failures.append(f"response contains forbidden text '{forbidden}'")

    for check in assertions.get("db_checks", []):
        q = substitute(check["query"], ctx)
        rows = run_query(conn, q)
        min_rows = check.get("expect_min_rows", 1)
        if len(rows) < min_rows:
            failures.append(f"db_check returned {len(rows)} rows, expected >= {min_rows}: {q}")

    result = {"id": case_id, "result": "PASS" if not failures else "FAIL", "reason": "; ".join(failures) or "ok"}
    return _with_forbidden_checks(case, ctx, conn, result)


def _with_forbidden_checks(case, ctx, conn, result):
    for check in case.get("assertions", {}).get("forbidden_db_checks", []):
        q = substitute(check["query"], ctx)
        rows = run_query(conn, q)
        if rows:
            result["result"] = "FAIL"
            result["reason"] = (result.get("reason", "") + f"; forbidden rows found: {q}").strip("; ")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        requests.get(f"{BASE_URL}/health", timeout=3)
    except requests.exceptions.RequestException:
        print(f"[FATAL] Cannot reach {BASE_URL}/health. Start the server first:")
        print("    uvicorn app.main:app --reload")
        sys.exit(1)

    cases = json.loads(CASES_PATH.read_text())
    conn = get_conn()

    results = []
    for case in cases:
        print(f"Running {case['id']}: {case.get('description', '')[:70]}")
        res = run_case(case, conn)
        res["group"] = case.get("group")
        res["manual_review"] = case.get("manual_review", False)
        print(f"  -> {res['result']}  ({res.get('reason', '')[:120]})")
        results.append(res)

    conn.close()

    passed = sum(1 for r in results if r["result"] == "PASS")
    failed = sum(1 for r in results if r["result"] == "FAIL")
    skipped = sum(1 for r in results if r["result"] == "SKIP")
    timed_out = sum(1 for r in results if r["result"] == "TIMEOUT")

    resolvable = [r for r in results if r["result"] in ("PASS", "FAIL")]
    verified_resolution_rate = (
        sum(1 for r in resolvable if r["result"] == "PASS") / len(resolvable) if resolvable else 0.0
    )

    guardrail_cases = [r for r in results if r["group"] == "guardrail" and r["result"] != "SKIP"]
    guardrail_precision = (
        sum(1 for r in guardrail_cases if r["result"] == "PASS") / len(guardrail_cases)
        if guardrail_cases
        else 0.0
    )

    report = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "timed_out": timed_out,
        "metrics": {
            "verified_resolution_rate": round(verified_resolution_rate, 3),
            "guardrail_precision": round(guardrail_precision, 3),
        },
        "cases": results,
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2))

    print("\n=== Summary ===")
    print(f"  PASS: {passed}  FAIL: {failed}  SKIP: {skipped}  TIMEOUT: {timed_out}  / {len(results)} total")
    print(f"  verified_resolution_rate: {report['metrics']['verified_resolution_rate']}")
    print(f"  guardrail_precision: {report['metrics']['guardrail_precision']}")
    print(f"\nResults written to {REPORT_PATH}")


if __name__ == "__main__":
    main()
