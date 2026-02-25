import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import SECRET_KEY
from app.models import OTPCode


def generate_otp() -> str:
    """Generate a 6-digit OTP code."""
    return f"{secrets.randbelow(1000000):06d}"


def hash_otp(code: str) -> str:
    """Hash OTP code with HMAC-SHA256."""
    return hmac.new(
        SECRET_KEY.encode(), code.encode(), hashlib.sha256
    ).hexdigest()


def verify_otp(code: str, code_hash: str) -> bool:
    """Verify OTP code against hash using constant-time comparison."""
    computed = hash_otp(code)
    return hmac.compare_digest(computed, code_hash)


def create_otp(db: Session, email: str) -> str | None:
    """Create and store a new OTP for the given email.

    Returns the plaintext code, or None if rate-limited.
    """
    email = email.lower().strip()

    # Rate limit: max 5 OTPs per email per hour
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    recent_count = (
        db.query(OTPCode)
        .filter(OTPCode.email == email, OTPCode.created_at >= one_hour_ago)
        .count()
    )
    if recent_count >= 5:
        return None

    code = generate_otp()
    otp = OTPCode(
        email=email,
        code_hash=hash_otp(code),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(otp)
    db.commit()
    return code


def validate_otp(db: Session, email: str, code: str) -> bool:
    """Validate an OTP code for the given email.

    Checks: not expired, not used, attempts < 5, code matches.
    Returns True on success, False on failure.
    """
    email = email.lower().strip()
    now = datetime.now(timezone.utc)

    # Get the most recent unused, non-expired OTP for this email
    otp = (
        db.query(OTPCode)
        .filter(
            OTPCode.email == email,
            OTPCode.used == False,
            OTPCode.expires_at > now,
            OTPCode.attempts < 5,
        )
        .order_by(OTPCode.created_at.desc())
        .first()
    )

    if not otp:
        return False

    if verify_otp(code, otp.code_hash):
        otp.used = True
        db.commit()
        return True
    else:
        otp.attempts += 1
        db.commit()
        return False
