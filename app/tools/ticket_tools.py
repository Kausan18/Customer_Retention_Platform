# app/tools/ticket_tools.py
"""
Write tool for ticket status updates. Called at the end of every
graph (all 5 workflows), success or failure path.
"""

from datetime import datetime, timezone
from app.db.database import fetch_one, execute


def update_ticket(ticket_id: str, status: str, note: str, customer_id: str) -> dict:
    """
    Updates a ticket's status and summary note. Ownership-checked:
    a ticket_id alone does not grant write access.
    """
    row = fetch_one(
        "SELECT id FROM tickets WHERE id = ? AND customer_id = ?",
        (ticket_id, customer_id),
    )
    if row is None:
        return {
            "success": False,
            "error_code": "TICKET_NOT_FOUND",
            "retryable": False,
            "message": "No ticket found for this customer with that ID.",
        }

    now = datetime.now(timezone.utc).isoformat()
    execute(
        "UPDATE tickets SET status = ?, summary = ?, updated_at = ? WHERE id = ?",
        (status, note, now, ticket_id),
    )

    return {"success": True, "ticket_id": ticket_id, "status": status}