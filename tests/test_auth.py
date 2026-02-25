from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import AdminUser, OTPCode
from app.services.otp import generate_otp, hash_otp, verify_otp, create_otp, validate_otp
from app.routes.auth import _state_serializer


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


# --- Google OAuth tests ---

def _make_state(role="admin"):
    """Create a valid signed OAuth state cookie value."""
    import secrets
    state_data = {"role": role, "nonce": secrets.token_urlsafe(16)}
    return _state_serializer.dumps(state_data)


def _mock_google_token_exchange(email):
    """Return a mock httpx.AsyncClient that simulates Google token + JWKS responses."""
    mock_client = AsyncMock()
    # Token endpoint response
    mock_token_resp = MagicMock()
    mock_token_resp.json.return_value = {"id_token": "fake.id.token", "access_token": "fake"}
    # JWKS endpoint response
    mock_jwks_resp = MagicMock()
    mock_jwks_resp.json.return_value = {"keys": []}
    mock_client.post = AsyncMock(return_value=mock_token_resp)
    mock_client.get = AsyncMock(return_value=mock_jwks_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    # Mock jose_jwt.decode to return claims with the email
    mock_claims = MagicMock()
    mock_claims.get.side_effect = lambda key, default="": email if key == "email" else default
    mock_claims.validate.return_value = None

    return mock_client, mock_claims


@pytest.mark.anyio
async def test_google_oauth_redirect():
    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", True), \
         patch("app.routes.auth.GOOGLE_CLIENT_ID", "test-client-id"):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            response = await client.get("/auth/google?role=admin")
            assert response.status_code == 302
            assert "accounts.google.com" in response.headers["location"]
            assert "oauth_state" in response.cookies


@pytest.mark.anyio
async def test_google_oauth_callback_admin_success(db):
    db.add(AdminUser(email="admin@test.com", is_active=True))
    db.commit()

    state = _make_state("admin")
    mock_client, mock_claims = _mock_google_token_exchange("admin@test.com")

    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", True), \
         patch("app.routes.auth.httpx.AsyncClient", return_value=mock_client), \
         patch("app.routes.auth.jose_jwt.decode", return_value=mock_claims):

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            response = await client.get(
                f"/auth/google/callback?state={state}&code=fake_code",
                cookies={"oauth_state": state},
            )
            assert response.status_code == 303
            assert response.headers["location"] == "/admin"
            assert "session" in response.cookies


@pytest.mark.anyio
async def test_google_oauth_callback_vendor_success(db):
    # Open registration so vendor login is allowed
    from app.models import EventSettings
    settings = db.query(EventSettings).first()
    if settings:
        settings.registration_open_date = datetime.now(timezone.utc) - timedelta(days=1)
        settings.registration_close_date = datetime.now(timezone.utc) + timedelta(days=30)
    else:
        from datetime import date
        db.add(EventSettings(
            event_name="Test",
            event_start_date=date.today(),
            event_end_date=date.today(),
            registration_open_date=datetime.now(timezone.utc) - timedelta(days=1),
            registration_close_date=datetime.now(timezone.utc) + timedelta(days=30),
            vendor_agreement_text="agree",
        ))
    db.commit()

    state = _make_state("vendor")
    mock_client, mock_claims = _mock_google_token_exchange("vendor@example.com")

    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", True), \
         patch("app.routes.auth.httpx.AsyncClient", return_value=mock_client), \
         patch("app.routes.auth.jose_jwt.decode", return_value=mock_claims):

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            response = await client.get(
                f"/auth/google/callback?state={state}&code=fake_code",
                cookies={"oauth_state": state},
            )
            assert response.status_code == 303
            assert response.headers["location"] == "/vendor/dashboard"
            assert "session" in response.cookies


@pytest.mark.anyio
async def test_google_oauth_callback_admin_rejected(db):
    # No admin user seeded — email not authorized
    state = _make_state("admin")
    mock_client, mock_claims = _mock_google_token_exchange("notadmin@example.com")

    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", True), \
         patch("app.routes.auth.httpx.AsyncClient", return_value=mock_client), \
         patch("app.routes.auth.jose_jwt.decode", return_value=mock_claims):

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            response = await client.get(
                f"/auth/google/callback?state={state}&code=fake_code",
                cookies={"oauth_state": state},
            )
            assert response.status_code == 403
            assert "not authorized" in response.text


@pytest.mark.anyio
async def test_google_oauth_callback_invalid_state():
    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", True):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            response = await client.get(
                "/auth/google/callback?state=bad_state&code=fake_code",
                cookies={"oauth_state": "different_state"},
            )
            assert response.status_code == 303
            assert "/auth/login" in response.headers["location"]


@pytest.mark.anyio
async def test_google_oauth_callback_google_error():
    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", True):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            response = await client.get("/auth/google/callback?error=access_denied")
            assert response.status_code == 303
            assert "/auth/login" in response.headers["location"]


@pytest.mark.anyio
async def test_google_oauth_disabled_when_no_env_vars():
    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", False):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            response = await client.get("/auth/google?role=admin")
            assert response.status_code == 303
            assert "/auth/login" in response.headers["location"]


@pytest.mark.anyio
async def test_google_button_shown_when_enabled():
    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", True):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/auth/login?role=admin")
            assert response.status_code == 200
            assert "Sign in with Google" in response.text


@pytest.mark.anyio
async def test_google_button_hidden_when_disabled():
    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", False):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/auth/login?role=admin")
            assert response.status_code == 200
            assert "Sign in with Google" not in response.text
