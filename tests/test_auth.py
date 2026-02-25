from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import AdminUser, OTPCode
from app.services.otp import generate_otp, hash_otp, verify_otp, create_otp, validate_otp


# --- OTP unit tests ---

def test_otp_generates_6_digits():
    for _ in range(100):
        code = generate_otp()
        assert len(code) == 6
        assert code.isdigit()


def test_hmac_hash_verify_roundtrip():
    code = "123456"
    h = hash_otp(code)
    assert verify_otp(code, h) is True


def test_hmac_wrong_code_rejected():
    code = "123456"
    h = hash_otp(code)
    assert verify_otp("654321", h) is False


def test_expired_otp_rejected(db):
    email = "test@example.com"
    code = "123456"
    otp = OTPCode(
        email=email,
        code_hash=hash_otp(code),
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),  # expired
    )
    db.add(otp)
    db.commit()

    assert validate_otp(db, email, code) is False


def test_used_otp_cannot_be_reused(db):
    email = "test@example.com"
    code = "123456"
    otp = OTPCode(
        email=email,
        code_hash=hash_otp(code),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        used=True,
    )
    db.add(otp)
    db.commit()

    assert validate_otp(db, email, code) is False


def test_max_5_attempts_enforced(db):
    email = "test@example.com"
    code = "123456"
    otp = OTPCode(
        email=email,
        code_hash=hash_otp(code),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        attempts=5,
    )
    db.add(otp)
    db.commit()

    # Even the correct code should be rejected after 5 attempts
    assert validate_otp(db, email, code) is False


def test_rate_limiting_max_5_per_hour(db):
    email = "test@example.com"

    # Create 5 OTPs successfully
    for i in range(5):
        result = create_otp(db, email)
        assert result is not None, f"OTP {i+1} should succeed"

    # 6th should be rate-limited
    result = create_otp(db, email)
    assert result is None


def test_valid_otp_succeeds(db):
    email = "test@example.com"
    code = create_otp(db, email)
    assert code is not None
    assert validate_otp(db, email, code) is True


def test_otp_attempt_increments(db):
    email = "test@example.com"
    code = create_otp(db, email)
    assert code is not None

    # Wrong code increments attempts
    assert validate_otp(db, email, "000000") is False
    otp = db.query(OTPCode).filter(OTPCode.email == email).first()
    assert otp.attempts == 1


# --- HTTP route tests ---

@pytest.mark.anyio
async def test_health_check():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_unauthenticated_admin_redirects_to_login():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get("/admin")
        assert response.status_code == 303
        assert "/auth/login" in response.headers.get("location", "")


@pytest.mark.anyio
async def test_non_admin_cannot_access_admin(db):
    # Create a valid session for a non-admin user
    from app.session import _serializer
    import time

    session_data = {
        "user_type": "vendor",
        "email": "vendor@test.com",
        "created_at": time.time(),
        "last_activity": time.time(),
    }
    cookie_value = _serializer.dumps(session_data)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get("/admin", cookies={"session": cookie_value})
        assert response.status_code == 403


@pytest.mark.anyio
async def test_login_page_loads():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Use role=admin since vendor login redirects when registration is not open
        response = await client.get("/auth/login?role=admin")
        assert response.status_code == 200
        assert "Login" in response.text


@pytest.mark.anyio
async def test_login_sends_otp(db):
    # Seed an admin user so the admin login check passes
    from app.models import AdminUser
    db.add(AdminUser(email="admin@test.com", is_active=True))
    db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        # Use role=admin since vendor login redirects when registration is not open
        login_page = await client.get("/auth/login?role=admin")
        # Extract csrf_token from form
        import re
        match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
        assert match, "CSRF token not found in login page"
        csrf_token = match.group(1)

        with patch("app.routes.auth.send_otp_email", return_value=True):
            response = await client.post(
                "/auth/login",
                data={"email": "admin@test.com", "csrf_token": csrf_token, "role": "admin"},
            )
            assert response.status_code == 200
            assert "Verification Code" in response.text
