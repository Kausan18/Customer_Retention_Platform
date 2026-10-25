# app/services/audit.py
"""
Audit event logger.
Extends the base-version AuditLogger into a module-level function
importable by any API or service without instantiation.
"""

import json
import uuid
from datetime import datetime, timezone

from app.db.database import execute


def log_event(
    run_id: str | None,
    actor: str,
    event_type: str,
    payload: dict | None = None,
) -> None:
    """
    Append one audit event to audit_events.
    Payload is stored as redacted JSON — never include raw PII.
    """
    event_id = f"evt_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload_json = json.dumps(payload or {}, separators=(",", ":"))

    execute(
        """
        INSERT INTO audit_events (id, run_id, actor, event_type, payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (event_id, run_id, actor, event_type, payload_json, now),
    )