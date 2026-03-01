"""Tier 6 — Google OAuth tests (Stories 34–37).

Tests mock all external Google endpoints (token exchange, JWKS, JWT decode)
so they run without real OAuth credentials.  GOOGLE_OAUTH_ENABLED is patched
per-test.
"""

import secrets
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from app.routes.auth import _state_serializer
from tests.helpers import seed_admin, seed_event_open

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(role="vendor"):
    """Create a signed OAuth state with embedded nonce."""
    nonce = secrets.token_urlsafe(16)
    state_data = {"role": role, "nonce": nonce}
    state = _state_serializer.dumps(state_data)
    return state, nonce


def _mock_google_http(token_resp_json=None):
    """Build a mock httpx.AsyncClient for Google token + JWKS endpoints."""
    if token_resp_json is None:
        token_resp_json = {
            "access_token": "mock_access",
            "id_token": "mock_id_token",
        }
    token_resp = MagicMock()
    token_resp.json.return_value = token_resp_json

    jwks_resp = MagicMock()
    jwks_resp.json.return_value = {"keys": []}

    mock_client = AsyncMock()
    mock_client.post.return_value = token_resp
    mock_client.get.return_value = jwks_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _mock_claims(email, nonce):
    """Build a mock JWT claims object with .validate() and dict-like .get()."""
    data = {"nonce": nonce, "email": email}
    claims = MagicMock()
    claims.get.side_effect = lambda k, d="": data.get(k, d)
    claims.validate.return_value = None
    return claims


def _oauth_patches(mock_http, mock_jwt):
    """Return a combined context manager enabling OAuth with mocked externals."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", True))
    stack.enter_context(patch("app.routes.auth.GOOGLE_CLIENT_ID", "test-client-id"))
    stack.enter_context(patch("app.routes.auth.GOOGLE_CLIENT_SECRET", "test-secret"))
    stack.enter_context(patch("httpx.AsyncClient", return_value=mock_http))
    stack.enter_context(patch("app.routes.auth.jose_jwt.decode", return_value=mock_jwt))
    return stack


# ---------------------------------------------------------------------------
# Story 34: Vendor logs in via Google OAuth
# ---------------------------------------------------------------------------


async def test_story34_google_login_redirects_to_google(client, db):
    """GET /auth/google?role=vendor redirects to Google with correct params."""
    seed_event_open(db)
    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", True), \
         patch("app.routes.auth.GOOGLE_CLIENT_ID", "test-client-id"):
        resp = await client.get("/auth/google?role=vendor")

    assert resp.status_code == 302
    location = resp.headers["location"]
    parsed = urlparse(location)
    params = parse_qs(parsed.query)

    assert "accounts.google.com" in parsed.hostname
    assert params["client_id"] == ["test-client-id"]
    assert params["response_type"] == ["code"]
    assert "openid" in params["scope"][0]
    assert "email" in params["scope"][0]
    assert "state" in params
    # State cookie set for CSRF verification in callback
    assert "oauth_state" in resp.cookies


async def test_story34_google_callback_creates_vendor_session(client, db):
    """Callback with valid code and state creates vendor session."""
    seed_event_open(db)
    email = "vendor-oauth@test.com"
    state, nonce = _make_state("vendor")

    mock_http = _mock_google_http()
    mock_jwt = _mock_claims(email, nonce)

    with _oauth_patches(mock_http, mock_jwt):
        resp = await client.get(
            f"/auth/google/callback?code=test_code&state={state}",
            cookies={"oauth_state": state},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/vendor/dashboard"
    assert "session" in resp.cookies


# ---------------------------------------------------------------------------
# Story 35: Admin logs in via Google OAuth
# ---------------------------------------------------------------------------


async def test_story35_admin_google_login(client, db):
    """Admin callback creates admin session when email is in admin_users."""
    seed_event_open(db)
    email = "admin-oauth@test.com"
    seed_admin(db, email=email)
    state, nonce = _make_state("admin")

    mock_http = _mock_google_http()
    mock_jwt = _mock_claims(email, nonce)

    with _oauth_patches(mock_http, mock_jwt):
        resp = await client.get(
            f"/auth/google/callback?code=test_code&state={state}",
            cookies={"oauth_state": state},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"
    assert "session" in resp.cookies


async def test_story35_non_admin_email_rejected(client, db):
    """Admin OAuth login with non-admin email returns 403."""
    seed_event_open(db)
    email = "not-an-admin@test.com"
    state, nonce = _make_state("admin")

    mock_http = _mock_google_http()
    mock_jwt = _mock_claims(email, nonce)

    with _oauth_patches(mock_http, mock_jwt):
        resp = await client.get(
            f"/auth/google/callback?code=test_code&state={state}",
            cookies={"oauth_state": state},
        )

    assert resp.status_code == 403
    assert "session" not in resp.cookies


# ---------------------------------------------------------------------------
# Story 36: OAuth state cookie mismatch rejected
# ---------------------------------------------------------------------------


async def test_story36_state_mismatch_rejected(client, db):
    """Callback with mismatched state cookie redirects to login."""
    state, _ = _make_state("vendor")

    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", True):
        resp = await client.get(
            f"/auth/google/callback?code=test_code&state={state}",
            cookies={"oauth_state": "wrong-state-value"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    assert "session" not in resp.cookies


async def test_story36_missing_state_cookie_rejected(client, db):
    """Callback with no state cookie redirects to login."""
    state, _ = _make_state("vendor")

    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", True):
        resp = await client.get(
            f"/auth/google/callback?code=test_code&state={state}",
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


async def test_story36_nonce_mismatch_rejected(client, db):
    """Callback with valid state but mismatched nonce in ID token is rejected."""
    seed_event_open(db)
    state, _nonce = _make_state("vendor")

    mock_http = _mock_google_http()
    # Return claims with a DIFFERENT nonce
    mock_jwt = _mock_claims("vendor@test.com", "wrong-nonce")

    with _oauth_patches(mock_http, mock_jwt):
        resp = await client.get(
            f"/auth/google/callback?code=test_code&state={state}",
            cookies={"oauth_state": state},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    assert "session" not in resp.cookies


async def test_story36_google_returns_error(client, db):
    """Callback with error param from Google redirects to login."""
    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", True):
        resp = await client.get(
            "/auth/google/callback?error=access_denied",
        )

    assert resp.status_code == 303
    assert "/auth/login" in resp.headers["location"]


async def test_story36_no_auth_code_rejected(client, db):
    """Callback without authorization code redirects to login."""
    state, _ = _make_state("vendor")

    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", True):
        resp = await client.get(
            f"/auth/google/callback?state={state}",
            cookies={"oauth_state": state},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


async def test_story36_token_exchange_error_rejected(client, db):
    """Callback where Google token exchange returns an error redirects to login."""
    seed_event_open(db)
    state, nonce = _make_state("vendor")

    mock_http = _mock_google_http(token_resp_json={"error": "invalid_grant"})
    mock_jwt = _mock_claims("vendor@test.com", nonce)

    with _oauth_patches(mock_http, mock_jwt):
        resp = await client.get(
            f"/auth/google/callback?code=test_code&state={state}",
            cookies={"oauth_state": state},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# Story 37: OAuth disabled returns error
# ---------------------------------------------------------------------------


async def test_story37_oauth_disabled_login_redirects(client, db):
    """GET /auth/google with OAuth disabled redirects to login page."""
    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", False):
        resp = await client.get("/auth/google?role=vendor")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


async def test_story37_oauth_disabled_callback_redirects(client, db):
    """Callback with OAuth disabled redirects to login page."""
    with patch("app.routes.auth.GOOGLE_OAUTH_ENABLED", False):
        resp = await client.get("/auth/google/callback?code=test&state=test")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
