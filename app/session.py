import json
import time
from typing import Optional

from fastapi import Request, Response, Depends, HTTPException
from itsdangerous import URLSafeTimedSerializer, BadSignature
from sqlalchemy.orm import Session

from app.config import SECRET_KEY, DEBUG
from app.database import get_db
from app.models import AdminUser


def get_client_ip(request: Request) -> str:
    """Extract real client IP, respecting X-Forwarded-For behind a reverse proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # X-Forwarded-For: client, proxy1, proxy2 — take the first (leftmost)
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

COOKIE_NAME = "session"
VENDOR_TIMEOUT = 24 * 60 * 60  # 24 hours
ADMIN_TIMEOUT = 8 * 60 * 60    # 8 hours

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="session-cookie")


def create_session(response: Response, user_type: str, email: str) -> None:
    """Create a signed session cookie."""
    timeout = ADMIN_TIMEOUT if user_type == "admin" else VENDOR_TIMEOUT
    data = {
        "user_type": user_type,
        "email": email,
        "created_at": time.time(),
        "last_activity": time.time(),
    }
    signed = _serializer.dumps(data)
    response.set_cookie(
        key=COOKIE_NAME,
        value=signed,
        httponly=True,
        secure=not DEBUG,
        samesite="lax",
        path="/",
        max_age=timeout,
    )


def read_session(request: Request) -> Optional[dict]:
    """Read and validate session cookie. Returns session data or None."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None

    try:
        data = _serializer.loads(cookie)  # validate signature only
    except BadSignature:
        return None

    # Check role-specific max age
    timeout = ADMIN_TIMEOUT if data.get("user_type") == "admin" else VENDOR_TIMEOUT
    created_at = data.get("created_at", 0)
    if time.time() - created_at > timeout:
        return None

    # Check inactivity timeout
    if time.time() - data.get("last_activity", 0) > timeout:
        return None

    return data


def refresh_session(response: Response, session_data: dict) -> None:
    """Refresh last_activity timestamp in session cookie."""
    timeout = ADMIN_TIMEOUT if session_data.get("user_type") == "admin" else VENDOR_TIMEOUT
    session_data["last_activity"] = time.time()
    signed = _serializer.dumps(session_data)
    response.set_cookie(
        key=COOKIE_NAME,
        value=signed,
        httponly=True,
        secure=not DEBUG,
        samesite="lax",
        path="/",
        max_age=timeout,
    )


def clear_session(response: Response) -> None:
    """Delete session cookie."""
    response.delete_cookie(key=COOKIE_NAME, path="/")


def require_admin(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """FastAPI dependency: requires active admin session."""
    session = read_session(request)
    if not session:
        raise HTTPException(status_code=303, headers={"Location": "/auth/login?role=admin"})

    if session.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    admin = (
        db.query(AdminUser)
        .filter(AdminUser.email == session["email"], AdminUser.is_active == True)
        .first()
    )
    if not admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    return session


def require_vendor(request: Request) -> dict:
    """FastAPI dependency: requires vendor session. Redirects to login if missing."""
    session = read_session(request)
    if not session:
        raise HTTPException(status_code=303, headers={"Location": "/auth/login"})
    if session.get("user_type") != "vendor":
        raise HTTPException(status_code=303, headers={"Location": "/auth/login"})
    return session
