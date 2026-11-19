# app/auth/session.py
"""
Session management for V1.

Tokens are stored in process memory — acceptable for V1/demo scope.
Sessions do not persist across restarts.
Each token maps to one identity and role.
FastAPI endpoints import require_session as a Depends() dependency.
"""

import secrets
from datetime import datetime
from fastapi import Request, HTTPException, status

# token → {"customer_id": str, "role": str, "created_at": str}
_sessions: dict[str, dict] = {}


def create_session(customer_id: str, role: str = "customer") -> str:
    """Create a new session token for an authenticated user."""
    token = secrets.token_hex(32)
    _sessions[token] = {
        "customer_id": customer_id,
        "role": role,
        "created_at": datetime.utcnow().isoformat(),
    }
    return token


def get_customer_id(token: str) -> dict[str, str] | None:
    """Return the identity and role for a valid token, or None."""
    entry = _sessions.get(token)
    if entry is None:
        return None
    return {"customer_id": entry["customer_id"], "role": entry["role"]}


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
    session = get_customer_id(token)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token.",
        )
    return session["customer_id"]


def require_reviewer_session(request: Request) -> str:
    """FastAPI dependency that requires a valid reviewer session."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
        )
    token = auth_header[len("Bearer "):]
    session = get_customer_id(token)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token.",
        )
    if session["role"] != "reviewer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Reviewer access required.",
        )
    return session["customer_id"]


def _clear_all_sessions_for_testing() -> None:
    """Test helper only. Clears all sessions between test cases."""
    _sessions.clear()