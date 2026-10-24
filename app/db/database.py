"""
app/db/database.py

Thin SQLite connection layer shared by every tool module
(order_tools, payment_tools, shipping_tools, ticket_tools, etc.).

Responsibilities, and only these:
  - know where the database file lives
  - open connections with foreign_keys ON and row_factory = Row
  - provide a context manager that commits on success, rolls back on
    exception, and always closes
  - provide small parameterized-query helpers so callers never build
    SQL by string-formatting user input

No business logic, no eligibility rules, no guardrail logic lives here.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DB_PATH = DATA_DIR / "customer_support.sqlite3"


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found at {DB_PATH}. Run `python -m app.db.seed` first."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """
    Usage:
        with get_connection() as conn:
            conn.execute("INSERT INTO ...", (...))
        # commits automatically on clean exit, rolls back on exception

    For read-only call sites, prefer fetch_one / fetch_all below instead
    of opening a connection directly.
    """
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_one(query: str, params: Sequence[Any] = ()) -> dict | None:
    """
    Run a parameterized SELECT and return the first row as a dict,
    or None if there are no results.

    Always pass params positionally via `?` placeholders in `query`.
    Never interpolate values into the query string.
    """
    with get_connection() as conn:
        row = conn.execute(query, params).fetchone()
        return dict(row) if row is not None else None


def fetch_all(query: str, params: Sequence[Any] = ()) -> list[dict]:
    """
    Run a parameterized SELECT and return all rows as a list of dicts.
    """
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def execute(query: str, params: Sequence[Any] = ()) -> int:
    """
    Run a parameterized INSERT/UPDATE/DELETE. Returns the cursor's
    lastrowid (not meaningful for our TEXT primary keys, but returned
    for completeness) and commits via the context manager.
    """
    with get_connection() as conn:
        cur = conn.execute(query, params)
        return cur.lastrowid


def execute_many(statements: Sequence[tuple[str, Sequence[Any]]]) -> None:
    """
    Run multiple parameterized statements in a single transaction.
    Useful for tool calls that must write to more than one table
    atomically (e.g. creating a ticket + its first audit event).

    statements: a sequence of (query, params) pairs, executed in order.
    All succeed or all roll back together.
    """
    with get_connection() as conn:
        for query, params in statements:
            conn.execute(query, params)


def row_exists(table: str, where_column: str, value: Any) -> bool:
    """
    Narrow existence check used by ownership/authorization guardrails,
    e.g. row_exists("orders", "id", order_id). `table` and
    `where_column` are never user-controlled — only `value` is a
    parameter — so this stays injection-safe as long as callers pass
    literal table/column names from code, not user input.
    """
    query = f"SELECT 1 FROM {table} WHERE {where_column} = ? LIMIT 1"  # noqa: S608
    with get_connection() as conn:
        return conn.execute(query, (value,)).fetchone() is not None