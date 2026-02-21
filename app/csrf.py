import secrets

from fastapi import Form, HTTPException
from itsdangerous import URLSafeSerializer

from app.config import SECRET_KEY

_serializer = URLSafeSerializer(SECRET_KEY, salt="csrf-token")


def generate_csrf_token() -> str:
    """Generate a signed CSRF token."""
    return _serializer.dumps(secrets.token_hex(16))


def validate_csrf_token(token: str) -> bool:
    """Validate a signed CSRF token. Returns True if valid."""
    try:
        _serializer.loads(token)
        return True
    except Exception:
        return False


def require_csrf(csrf_token: str = Form(...)):
    """FastAPI dependency to validate CSRF token on form submissions."""
    if not validate_csrf_token(csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
