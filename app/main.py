from __future__ import annotations

import csv
import hmac
import json
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from fastapi import FastAPI
from app.api import tickets, runs, approvals
from app.auth.session import create_session, invalidate_session
from app.db.database import fetch_one
from app.services.gemini_client import GeminiChatClient, call_gemini
import hmac, os
from pydantic import BaseModel


from app.guardrails.input_screener import (
    GuardrailDecision,
    clean_response,
    inspect_request,
    inspect_response,
    is_supported_question,
)

ROOT = Path(__file__).resolve().parent.parent
CSV_FILE = ROOT / "data" / "customer_shopping_data.csv" / "customer_shopping_data.csv"
DATABASE_FILE = ROOT / "data" / "customer_shopping.sqlite3"
LOG_FILE = ROOT / "logs" / "guardrail_audit.jsonl"
ENV_FILE = ROOT / ".env"
RATE_LIMIT_REQUESTS = 8
RATE_LIMIT_WINDOW_SECONDS = 60
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_GEMINI_TIMEOUT_SECONDS = 120
EXPECTED_CSV_COLUMNS = (
    "invoice_no",
    "customer_id",
    "gender",
    "age",
    "category",
    "quantity",
    "price",
    "payment_method",
    "invoice_date",
    "shopping_mall",
)


def load_environment(path: Path = ENV_FILE) -> None:
    """Load KEY=value entries from .env without overriding existing environment."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def prompt_demo_pin() -> str:
    """Read the PIN using standard terminal input, which works in VS Code terminals."""
    return input("Demo PIN [1234]: ").strip()


class ShoppingDatabase:
    """Keep a local SQLite copy of the CSV and return only customer-scoped summaries."""

    def __init__(self, csv_path: Path, database_path: Path) -> None:
        self.csv_path = csv_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        try:
            self._refresh_from_csv()
        except (OSError, csv.Error, KeyError, TypeError, ValueError, sqlite3.Error):
            self.connection.close()
            raise

    def _refresh_from_csv(self) -> None:
        if not self.csv_path.is_file():
            raise FileNotFoundError(f"Customer dataset not found: {self.csv_path}")

        source = self.csv_path.stat()
        source_mtime = str(source.st_mtime_ns)
        source_size = str(source.st_size)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS purchases ("
            "customer_id TEXT NOT NULL, category TEXT NOT NULL, "
            "quantity INTEGER NOT NULL, price REAL NOT NULL, "
            "payment_method TEXT NOT NULL, shopping_mall TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS purchases_customer_id "
            "ON purchases(customer_id)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS dataset_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        metadata = dict(
            self.connection.execute(
                "SELECT key, value FROM dataset_metadata"
            ).fetchall()
        )
        if (
            metadata.get("source_mtime_ns") == source_mtime
            and metadata.get("source_size") == source_size
        ):
            return

        with self.csv_path.open(encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            if tuple(reader.fieldnames or ()) != EXPECTED_CSV_COLUMNS:
                raise ValueError(
                    "Customer CSV columns do not match the expected dataset schema."
                )
            try:
                self.connection.execute("BEGIN")
                self.connection.execute("DELETE FROM purchases")
                self.connection.executemany(
                    "INSERT INTO purchases VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        (
                            row["customer_id"],
                            row["category"],
                            int(row["quantity"]),
                            float(row["price"]),
                            row["payment_method"],
                            row["shopping_mall"],
                        )
                        for row in reader
                    ),
                )
                self.connection.executemany(
                    "INSERT OR REPLACE INTO dataset_metadata(key, value) "
                    "VALUES (?, ?)",
                    (
                        ("source_mtime_ns", source_mtime),
                        ("source_size", source_size),
                    ),
                )
                self.connection.commit()
            except (KeyError, TypeError, ValueError, sqlite3.Error):
                self.connection.rollback()
                raise

    def customer_exists(self, customer_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM purchases WHERE customer_id = ? LIMIT 1",
                (customer_id,),
            ).fetchone()
            is not None
        )

    def permitted_context(self, customer_id: str) -> dict[str, Any]:
        """Aggregate only the authenticated customer's purchases."""
        totals = self.connection.execute(
            "SELECT COUNT(*) AS purchase_count, "
            "COALESCE(SUM(quantity), 0) AS units_purchased, "
            "COALESCE(SUM(price), 0) AS total_spend, "
            "COALESCE(AVG(price), 0) AS average_purchase_value "
            "FROM purchases WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
        context: dict[str, Any] = {
            "purchase_count": totals["purchase_count"],
            "units_purchased": totals["units_purchased"],
            "total_spend": round(totals["total_spend"], 2),
            "average_purchase_value": round(totals["average_purchase_value"], 2),
        }
        for field, result_key in (
            ("category", "spend_by_category"),
            ("payment_method", "spend_by_payment_method"),
            ("shopping_mall", "spend_by_mall"),
        ):
            rows = self.connection.execute(
                f"SELECT {field}, COUNT(*) AS purchase_count, "
                "COALESCE(SUM(quantity), 0) AS units_purchased, "
                "COALESCE(SUM(price), 0) AS total_spend "
                f"FROM purchases WHERE customer_id = ? GROUP BY {field} "
                "ORDER BY total_spend DESC",
                (customer_id,),
            ).fetchall()
            context[result_key] = [
                {
                    "name": row[field],
                    "purchase_count": row["purchase_count"],
                    "units_purchased": row["units_purchased"],
                    "total_spend": round(row["total_spend"], 2),
                }
                for row in rows
            ]
        return context

    def close(self) -> None:
        self.connection.close()


class AuditLogger:
    def log(self, customer_id: str, event: str, outcome: str, reason: str) -> None:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "customer_id": customer_id,
            "event": event,
            "outcome": outcome,
            "reason": reason,
        }
        with LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry) + "\n")


class RateLimiter:
    def __init__(self) -> None:
        self.requests: dict[str, deque[float]] = defaultdict(deque)

    def check(self, customer_id: str) -> bool:
        now = time.monotonic()
        requests = self.requests[customer_id]
        while requests and now - requests[0] > RATE_LIMIT_WINDOW_SECONDS:
            requests.popleft()
        if len(requests) >= RATE_LIMIT_REQUESTS:
            return False
        requests.append(now)
        return True


@dataclass(frozen=True)
class AuthenticatedSession:
    customer_id: str
    token: str


class ShoppingChatbot:
    def __init__(
        self,
        api_key: str,
        model: str,
        csv_path: Path = CSV_FILE,
        database_path: Path = DATABASE_FILE,
        demo_pin: str = "1234",
        gemini_timeout_seconds: int = DEFAULT_GEMINI_TIMEOUT_SECONDS,
    ) -> None:
        if not demo_pin or not demo_pin.isascii():
            raise ValueError("DEMO_PIN must be non-empty ASCII text.")
        self.data = ShoppingDatabase(csv_path, database_path)
        self.demo_pin = demo_pin
        self.audit = AuditLogger()
        self.limiter = RateLimiter()
        self.client = GeminiChatClient(api_key, model, gemini_timeout_seconds)
        self._sessions: dict[str, str] = {}

    def login(self, customer_id: str, pin: str) -> AuthenticatedSession | None:
        normalized_id = customer_id.strip().upper()
        if not self.data.customer_exists(normalized_id):
            return None
        if not pin.isascii() or not hmac.compare_digest(pin, self.demo_pin):
            return None
        token = secrets.token_urlsafe(32)
        self._sessions[token] = normalized_id
        return AuthenticatedSession(normalized_id, token)

    def logout(self, session: AuthenticatedSession) -> None:
        self._sessions.pop(session.token, None)

    def answer(
        self, session: AuthenticatedSession, prompt: str
    ) -> tuple[GuardrailDecision, str]:
        customer_id = self._sessions.get(session.token)
        if customer_id is None or customer_id != session.customer_id:
            return (
                GuardrailDecision("BLOCK", "", "authentication_required"),
                "Please log in before asking about purchase data.",
            )

        decision = inspect_request(prompt, customer_id)
        self.audit.log(customer_id, "input_screen", decision.action, decision.reason)
        if decision.action == "BLOCK":
            return decision, "I can't help with that request."
        if not is_supported_question(decision.text):
            self.audit.log(customer_id, "request_validation", "BLOCK", "unsupported_topic")
            return (
                GuardrailDecision("BLOCK", "", "unsupported_topic"),
                "I can answer questions about your purchase totals, categories, "
                "payment methods, or shopping malls.",
            )
        if not self.limiter.check(customer_id):
            self.audit.log(customer_id, "rate_limit", "BLOCK", "rate_limit_exceeded")
            return (
                GuardrailDecision("BLOCK", "", "rate_limit_exceeded"),
                "Too many requests. Please wait before trying again.",
            )

        response = self.client.answer(
            decision.text,
            self.data.permitted_context(customer_id),
        )
        response = clean_response(response)
        if inspect_response(response):
            self.audit.log(
                customer_id, "output_check", "BLOCK", "possible_sensitive_data"
            )
            return (
                GuardrailDecision("BLOCK", "", "possible_sensitive_data"),
                "I can't display that information.",
            )

        self.audit.log(customer_id, "chat_request", decision.action, "response_checked")
        return decision, response


def main() -> None:
    load_environment()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "your_gemini_api_key_here":
        raise SystemExit(
            "Add your Gemini API key to .env as GEMINI_API_KEY=your_key, then run app.py."
        )

    model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
    demo_pin = os.environ.get("DEMO_PIN", "1234")
    try:
        gemini_timeout_seconds = int(
            os.environ.get(
                "GEMINI_TIMEOUT_SECONDS",
                str(DEFAULT_GEMINI_TIMEOUT_SECONDS),
            )
        )
    except ValueError as error:
        raise SystemExit("GEMINI_TIMEOUT_SECONDS must be a positive integer.") from error
    if gemini_timeout_seconds <= 0:
        raise SystemExit("GEMINI_TIMEOUT_SECONDS must be a positive integer.")
    chatbot = ShoppingChatbot(
        api_key,
        model,
        demo_pin=demo_pin,
        gemini_timeout_seconds=gemini_timeout_seconds,
    )
    session: AuthenticatedSession | None = None

    print("CUSTOMER SHOPPING ASSISTANT — Gemini chatbot")
    print("Log in with a customer ID from the CSV and the demo PIN.")
    print("Only per-customer purchase summaries are sent to Gemini.")
    try:
        while True:
            if session is None:
                try:
                    customer_id = input("Customer ID (or /quit): ").strip()
                    if customer_id.lower() in {"/quit", "/exit"}:
                        break
                    pin = prompt_demo_pin()
                except (EOFError, KeyboardInterrupt):
                    print("\nExiting.")
                    break
                session = chatbot.login(customer_id, pin)
                if session is None:
                    print("Login failed. Check the customer ID and demo PIN.")
                    continue
                print(f"[DEMO LOGIN] Signed in as {session.customer_id}.")
                print("Type /help for commands or /logout to switch customers.")
                continue

            try:
                prompt = input(f"\n[{session.customer_id}] You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                break
            if not prompt:
                continue
            if prompt.lower() in {"/quit", "/exit"}:
                break
            if prompt.lower() == "/logout":
                chatbot.logout(session)
                session = None
                print("Logged out.")
                continue
            if prompt.lower() == "/help":
                print(
                    "Ask about your purchase totals, categories, payment methods, "
                    "or shopping malls."
                )
                continue

            try:
                decision, response = chatbot.answer(session, prompt)
            except RuntimeError as error:
                chatbot.audit.log(
                    session.customer_id, "llm_request", "ERROR", "api_request_failed"
                )
                print(f"[ERROR] {error}")
                continue
            print(f"[{decision.action}] {response}")
            if decision.action == "SANITIZE":
                print("(Input was normalized before it was sent to the model.)")
    finally:
        chatbot.data.close()
    print("Goodbye.")

load_environment(Path(__file__).resolve().parent.parent / ".env")
app = FastAPI(title="Customer Support Agent", version="1.0.0")
app.include_router(tickets.router)
app.include_router(runs.router)
app.include_router(approvals.router)


class LoginRequest(BaseModel):
    customer_id: str
    pin: str


@app.post("/auth/login")
def login(body: LoginRequest):
    from fastapi import HTTPException, status as http_status
    import unicodedata

    customer_id = unicodedata.normalize("NFKC", body.customer_id.strip()).upper()
    pin = body.pin.strip()

    # Verify customer exists in the seeded support database.
    row = fetch_one("SELECT customer_id FROM orders WHERE customer_id = ? LIMIT 1", (customer_id,))
    if row is None:
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Customer not found.")

    expected_pin = os.environ.get("DEMO_PIN", "1234")
    if not hmac.compare_digest(pin, expected_pin):
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Invalid PIN.")

    token = create_session(customer_id)
    return {"token": token, "customer_id": customer_id}


@app.post("/auth/logout")
def logout(request):
    from fastapi import Request
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        invalidate_session(auth[7:])
    return {"status": "logged out"}


@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    main()
