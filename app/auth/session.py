# app/auth/session.py
"""
Session management for V1.

Tokens are stored in process memory — acceptable for V1/demo scope.
Sessions do not persist across restarts.
Each token maps to exactly one customer_id.
FastAPI endpoints import require_session as a Depends() dependency.
"""

import secrets
from datetime import datetime
from fastapi import Request, HTTPException, status

# token → {"customer_id": str, "created_at": datetime}
_sessions: dict[str, dict] = {}


def create_session(customer_id: str) -> str:
    """Create a new session token for an authenticated customer."""
    token = secrets.token_hex(32)
    _sessions[token] = {
        "customer_id": customer_id,
        "created_at": datetime.utcnow().isoformat(),
    }
    return token


def get_customer_id(token: str) -> str | None:
    """Return the customer_id for a valid token, or None."""
    entry = _sessions.get(token)
    return entry["customer_id"] if entry else None


def invalidate_session(token: str) -> None:
    """Remove a session token (logout)."""
    _sessions.pop(token, None)


def require_session(request: Request) -> str:
    """
    FastAPI dependency. Reads Authorization: Bearer <token> header.
    Returns customer_id on success. Raises 401 on missing/invalid token.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
        )
    token = auth_header[len("Bearer "):]
    customer_id = get_customer_id(token)
    if customer_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token.",
        )
    return customer_id


def _clear_all_sessions_for_testing() -> None:
    """Test helper only. Clears all sessions between test cases."""
    _sessions.clear()