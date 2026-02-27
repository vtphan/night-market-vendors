import secrets

from fastapi import Form, HTTPException
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

from app.config import SECRET_KEY

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="csrf-token")

CSRF_MAX_AGE = 3600  # 1 hour


def generate_csrf_token() -> str:
    """Generate a signed, timestamped CSRF token."""
    return _serializer.dumps(secrets.token_hex(16))


def validate_csrf_token(token: str) -> bool:
    """Validate a signed CSRF token. Returns True if valid and not expired."""
    try:
        _serializer.loads(token, max_age=CSRF_MAX_AGE)
        return True
    except (SignatureExpired, BadSignature, Exception):
        return False


def require_csrf(csrf_token: str = Form(...)):
    """FastAPI dependency to validate CSRF token on form submissions."""
    if not validate_csrf_token(csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
