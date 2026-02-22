"""Tests for error pages, Stripe failure handling, and admin transition flash messages."""

from unittest.mock import patch, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from tests.helpers import (
    admin_cookie, vendor_cookie, extract_csrf, seed_admin,
    seed_booth_types, seed_event_open as seed_event, make_registration,
)


# ========================================
# 404 Error Pages
# ========================================

@pytest.mark.anyio
async def test_404_html_returns_styled_error_page():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/nonexistent-page",
            headers={"Accept": "text/html"},
        )
        assert response.status_code == 404
        assert "Page Not Found" in response.text
        assert "Return to Home" in response.text


@pytest.mark.anyio
async def test_404_json_returns_json():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/nonexistent-page",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 404
        data = response.json()
        assert "detail" in data


# ========================================
# 500 Safety Net
# ========================================

@pytest.mark.anyio
async def test_500_html_shows_friendly_page():
    """Unhandled exceptions should render a friendly error page, not a traceback."""
    # Temporarily add a route that raises
    from fastapi import APIRouter
    _test_router = APIRouter()

    @_test_router.get("/test-500-boom")
    async def boom():
        raise RuntimeError("kaboom")

    app.include_router(_test_router)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/test-500-boom",
                headers={"Accept": "text/html"},
            )
            assert response.status_code == 500
            assert "Something Went Wrong" in response.text
            assert "kaboom" not in response.text
            assert "Traceback" not in response.text
    finally:
        app.routes[:] = [r for r in app.routes if getattr(r, "path", None) != "/test-500-boom"]


@pytest.mark.anyio
async def test_500_json_returns_generic_message():
    from fastapi import APIRouter
    _test_router = APIRouter()

    @_test_router.get("/test-500-json")
    async def boom():
        raise RuntimeError("kaboom")

    app.include_router(_test_router)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/test-500-json",
                headers={"Accept": "application/json"},
            )
            assert response.status_code == 500
            data = response.json()
            assert "detail" in data
            assert "kaboom" not in data["detail"]
    finally:
        app.routes[:] = [r for r in app.routes if getattr(r, "path", None) != "/test-500-json"]


# ========================================
# Stripe Payment Failure
# ========================================

@pytest.mark.anyio
async def test_stripe_payment_failure_returns_502(db):
    seed_event(db)
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="approved", email="vendor@test.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Get CSRF token
        detail_resp = await client.get(
            f"/vendor/registration/{reg.registration_id}",
            cookies=vendor_cookie(),
            headers={"Accept": "text/html"},
        )
        csrf = extract_csrf(detail_resp.text)

        with patch("app.routes.vendor.create_payment_intent", side_effect=Exception("Stripe is down")):
            response = await client.post(
                f"/vendor/registration/{reg.registration_id}/pay",
                cookies=vendor_cookie(),
                data={"csrf_token": csrf},
                headers={"Accept": "application/json"},
            )
            assert response.status_code == 502
            data = response.json()
            assert "temporarily unavailable" in data["error"]


# ========================================
# Stripe Refund Failure
# ========================================

@pytest.mark.anyio
async def test_stripe_refund_failure_shows_flash_error(db):
    seed_admin(db)
    seed_event(db)
    booths = seed_booth_types(db)
    reg = make_registration(
        db, booths[0].id, status="confirmed",
        stripe_pi_id="pi_test_123", amount_paid=15000,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        # Get CSRF token from detail page
        detail_resp = await client.get(
            f"/admin/registrations/{reg.registration_id}",
            cookies=admin_cookie(),
            headers={"Accept": "text/html"},
        )
        csrf = extract_csrf(detail_resp.text)

        with patch("app.routes.admin.create_refund", side_effect=Exception("Stripe refund error")):
            response = await client.post(
                f"/admin/registrations/{reg.registration_id}/cancel",
                cookies=admin_cookie(),
                data={"csrf_token": csrf, "refund_amount": "150.00"},
                headers={"Accept": "text/html"},
            )
            # Should render the detail page with flash error (200), not redirect
            assert response.status_code == 200
            assert "Refund failed" in response.text

        # Registration should still be confirmed
        db.refresh(reg)
        assert reg.status == "confirmed"


# ========================================
# Invalid Admin Transitions
# ========================================

@pytest.mark.anyio
async def test_approve_confirmed_registration_shows_flash_error(db):
    seed_admin(db)
    seed_event(db)
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="confirmed")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        detail_resp = await client.get(
            f"/admin/registrations/{reg.registration_id}",
            cookies=admin_cookie(),
            headers={"Accept": "text/html"},
        )
        csrf = extract_csrf(detail_resp.text)

        response = await client.post(
            f"/admin/registrations/{reg.registration_id}/approve",
            cookies=admin_cookie(),
            data={"csrf_token": csrf},
            headers={"Accept": "text/html"},
        )
        assert response.status_code == 200
        assert "Cannot approve" in response.text
