import json
import time
from typing import Optional

from fastapi import Request, Response, Depends, HTTPException
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy.orm import Session

from app.config import SECRET_KEY, DEBUG
from app.database import get_db
from app.models import AdminUser

COOKIE_NAME = "session"
VENDOR_TIMEOUT = 24 * 60 * 60  # 24 hours
ADMIN_TIMEOUT = 8 * 60 * 60    # 8 hours

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="session-cookie")


def create_session(response: Response, user_type: str, email: str) -> None:
    """Create a signed session cookie."""
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
    )


def read_session(request: Request) -> Optional[dict]:
    """Read and validate session cookie. Returns session data or None."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None

    try:
        # Max age = 24h (we check inactivity separately)
        data = _serializer.loads(cookie, max_age=VENDOR_TIMEOUT)
    except (BadSignature, SignatureExpired):
        return None

    # Check inactivity timeout
    timeout = ADMIN_TIMEOUT if data.get("user_type") == "admin" else VENDOR_TIMEOUT
    if time.time() - data.get("last_activity", 0) > timeout:
        return None

    return data


def refresh_session(response: Response, session_data: dict) -> None:
    """Refresh last_activity timestamp in session cookie."""
    session_data["last_activity"] = time.time()
    signed = _serializer.dumps(session_data)
    response.set_cookie(
        key=COOKIE_NAME,
        value=signed,
        httponly=True,
        secure=not DEBUG,
        samesite="lax",
        path="/",
    )


def clear_session(response: Response) -> None:
    """Delete session cookie."""
    response.delete_cookie(key=COOKIE_NAME, path="/")


def get_current_user(request: Request) -> Optional[dict]:
    """FastAPI dependency: returns session data or None."""
    return read_session(request)


def require_admin(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """FastAPI dependency: requires active admin session."""
    session = read_session(request)
    if not session:
        raise HTTPException(status_code=303, headers={"Location": "/auth/login"})

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
