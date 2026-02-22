"""Tests for error pages, Stripe failure handling, and admin transition flash messages."""

import re
import time
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import AdminUser, BoothType, EventSettings, Registration
from app.session import _serializer


# --- Helpers ---

def _admin_cookie(email="admin@test.com"):
    data = {
        "user_type": "admin",
        "email": email,
        "created_at": time.time(),
        "last_activity": time.time(),
    }
    return {"session": _serializer.dumps(data)}


def _vendor_cookie(email="vendor@test.com"):
    data = {
        "user_type": "vendor",
        "email": email,
        "created_at": time.time(),
        "last_activity": time.time(),
    }
    return {"session": _serializer.dumps(data)}


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token not found"
    return match.group(1)


def _seed_admin(db, email="admin@test.com"):
    existing = db.query(AdminUser).filter(AdminUser.email == email).first()
    if not existing:
        db.add(AdminUser(email=email, is_active=True))
        db.commit()


def _seed_booth_types(db):
    if db.query(BoothType).count() > 0:
        return db.query(BoothType).order_by(BoothType.sort_order).all()
    booths = [
        BoothType(name="Premium", description="Corner spot", total_quantity=20, price=15000, sort_order=1),
    ]
    db.add_all(booths)
    db.commit()
    return db.query(BoothType).order_by(BoothType.sort_order).all()


def _seed_event(db):
    if db.query(EventSettings).first():
        return
    db.add(EventSettings(
        id=1,
        event_name="Test Event",
        event_date=datetime(2026, 10, 17).date(),
        registration_open_date=datetime(2020, 1, 1),
        registration_close_date=datetime(2030, 12, 31, 23, 59, 59),
        vendor_agreement_text="Agreement text.",
    ))
    db.commit()


def _make_registration(db, booth_type_id, status="pending", email="vendor@test.com",
                       reg_id="ANM-2026-0001", stripe_pi_id=None, amount_paid=None):
    reg = Registration(
        registration_id=reg_id,
        email=email,
        business_name="Test Biz",
        contact_name="Test Vendor",
        phone="555-0100",
        category="food",
        description="Delicious food",
        cuisine_type="Thai",
        booth_type_id=booth_type_id,
        status=status,
        agreement_accepted_at=datetime.now(timezone.utc),
        agreement_ip_address="127.0.0.1",
        stripe_payment_intent_id=stripe_pi_id,
        amount_paid=amount_paid,
    )
    db.add(reg)
    db.commit()
    db.refresh(reg)
    return reg


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
    _seed_event(db)
    booths = _seed_booth_types(db)
    reg = _make_registration(db, booths[0].id, status="approved", email="vendor@test.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Get CSRF token
        detail_resp = await client.get(
            f"/vendor/registration/{reg.registration_id}",
            cookies=_vendor_cookie(),
            headers={"Accept": "text/html"},
        )
        csrf = _extract_csrf(detail_resp.text)

        with patch("app.routes.vendor.create_payment_intent", side_effect=Exception("Stripe is down")):
            response = await client.post(
                f"/vendor/registration/{reg.registration_id}/pay",
                cookies=_vendor_cookie(),
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
    _seed_admin(db)
    _seed_event(db)
    booths = _seed_booth_types(db)
    reg = _make_registration(
        db, booths[0].id, status="confirmed",
        stripe_pi_id="pi_test_123", amount_paid=15000,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        # Get CSRF token from detail page
        detail_resp = await client.get(
            f"/admin/registrations/{reg.registration_id}",
            cookies=_admin_cookie(),
            headers={"Accept": "text/html"},
        )
        csrf = _extract_csrf(detail_resp.text)

        with patch("app.routes.admin.create_refund", side_effect=Exception("Stripe refund error")):
            response = await client.post(
                f"/admin/registrations/{reg.registration_id}/cancel",
                cookies=_admin_cookie(),
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
    _seed_admin(db)
    _seed_event(db)
    booths = _seed_booth_types(db)
    reg = _make_registration(db, booths[0].id, status="confirmed")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        detail_resp = await client.get(
            f"/admin/registrations/{reg.registration_id}",
            cookies=_admin_cookie(),
            headers={"Accept": "text/html"},
        )
        csrf = _extract_csrf(detail_resp.text)

        response = await client.post(
            f"/admin/registrations/{reg.registration_id}/approve",
            cookies=_admin_cookie(),
            data={"csrf_token": csrf},
            headers={"Accept": "text/html"},
        )
        assert response.status_code == 200
        assert "Cannot approve" in response.text
