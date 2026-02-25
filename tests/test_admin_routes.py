"""Integration tests for admin routes: dashboard, registrations, approve/reject, inventory, settings, CSV export."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import BoothType, EventSettings, Registration
from tests.helpers import (
    admin_cookie, extract_csrf, seed_admin, seed_booth_types,
    seed_event, make_registration,
)


# ========================================
# Dashboard
# ========================================

@pytest.mark.anyio
async def test_admin_dashboard_shows_counts(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, status="pending", reg_id="ANM-2026-0001")
    make_registration(db, booths[0].id, status="approved", reg_id="ANM-2026-0002", email="b@test.com")
    make_registration(db, booths[0].id, status="rejected", reg_id="ANM-2026-0003", email="c@test.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin", cookies=admin_cookie())
        assert response.status_code == 200
        text = response.text
        # Should show counts
        assert "Pending" in text
        assert "Approved" in text
        assert "Rejected" in text


@pytest.mark.anyio
async def test_admin_dashboard_unauthenticated_redirects(db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get("/admin")
        assert response.status_code == 303
        assert "/auth/login" in response.headers["location"]


# ========================================
# Registration list + filtering
# ========================================

@pytest.mark.anyio
async def test_registration_list_shows_all(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, status="pending", reg_id="ANM-2026-0001", business_name="Alpha Biz")
    make_registration(db, booths[0].id, status="approved", reg_id="ANM-2026-0002",
                      email="b@test.com", business_name="Beta Biz")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/registrations", cookies=admin_cookie())
        assert response.status_code == 200
        assert "Alpha Biz" in response.text
        assert "Beta Biz" in response.text
        assert "2 registration" in response.text


@pytest.mark.anyio
async def test_registration_list_filter_by_status(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, status="pending", reg_id="ANM-2026-0001", business_name="Pending Biz")
    make_registration(db, booths[0].id, status="approved", reg_id="ANM-2026-0002",
                      email="b@test.com", business_name="Approved Biz")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/registrations?status=pending", cookies=admin_cookie())
        assert response.status_code == 200
        assert "Pending Biz" in response.text
        assert "Approved Biz" not in response.text
        assert "1 registration" in response.text


@pytest.mark.anyio
async def test_registration_list_filter_by_category(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, status="pending", reg_id="ANM-2026-0001", business_name="Food Place")
    reg2 = make_registration(db, booths[0].id, status="pending", reg_id="ANM-2026-0002",
                              email="b@test.com", business_name="Craft Shop")
    reg2.category = "merchandise"
    db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/registrations?category=merchandise", cookies=admin_cookie())
        assert response.status_code == 200
        assert "Craft Shop" in response.text
        assert "Food Place" not in response.text


@pytest.mark.anyio
async def test_registration_list_search(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, reg_id="ANM-2026-0001", business_name="Unique Noodle House")
    make_registration(db, booths[0].id, reg_id="ANM-2026-0002", email="b@test.com", business_name="Generic Shop")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/registrations?search=Noodle", cookies=admin_cookie())
        assert response.status_code == 200
        assert "Unique Noodle House" in response.text
        assert "Generic Shop" not in response.text


# ========================================
# Registration detail
# ========================================

@pytest.mark.anyio
async def test_registration_detail_page(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, reg_id="ANM-2026-0010", business_name="Detail Biz")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/registrations/ANM-2026-0010", cookies=admin_cookie())
        assert response.status_code == 200
        assert "Detail Biz" in response.text
        assert "ANM-2026-0010" in response.text
        assert "Approve" in response.text  # Action button


@pytest.mark.anyio
async def test_registration_detail_unknown_redirects(db):
    seed_admin(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get("/admin/registrations/ANM-9999-9999", cookies=admin_cookie())
        assert response.status_code == 303
        assert "/admin/registrations" in response.headers["location"]


# ========================================
# Approve
# ========================================

@pytest.mark.anyio
async def test_approve_registration(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, status="pending", reg_id="ANM-2026-0020")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        # Get CSRF from detail page
        detail = await client.get("/admin/registrations/ANM-2026-0020", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        with patch("app.routes.admin.send_approval_email", return_value=True) as mock_email:
            response = await client.post("/admin/registrations/ANM-2026-0020/approve", data={
                "csrf_token": csrf,
            }, cookies=admin_cookie())

        assert response.status_code == 303
        mock_email.assert_called_once()

    # Verify DB
    reg = db.query(Registration).filter(Registration.registration_id == "ANM-2026-0020").first()
    db.refresh(reg)
    assert reg.status == "approved"
    assert reg.approved_at is not None


@pytest.mark.anyio
async def test_approve_already_rejected_fails_gracefully(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, status="rejected", reg_id="ANM-2026-0021")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        detail = await client.get("/admin/registrations/ANM-2026-0021", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        with patch("app.routes.admin.send_approval_email") as mock_email:
            response = await client.post("/admin/registrations/ANM-2026-0021/approve", data={
                "csrf_token": csrf,
            }, cookies=admin_cookie())

        assert response.status_code == 200
        assert "Cannot approve" in response.text
        mock_email.assert_not_called()  # Email should NOT be sent

    reg = db.query(Registration).filter(Registration.registration_id == "ANM-2026-0021").first()
    db.refresh(reg)
    assert reg.status == "rejected"  # Unchanged


@pytest.mark.anyio
async def test_approve_blocked_when_sold_out(db):
    """Admin cannot approve when booth type has zero availability."""
    seed_admin(db)
    booths = seed_booth_types(db)
    bt = booths[0]  # Premium: total_quantity=20

    # Fill all 20 slots with approved/paid registrations
    for i in range(20):
        make_registration(
            db, bt.id, status="approved",
            reg_id=f"ANM-2026-0F{i:02d}", email=f"fill{i}@test.com",
        )

    # Create one more pending registration
    make_registration(db, bt.id, status="pending", reg_id="ANM-2026-0F99", email="overflow@test.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        detail = await client.get("/admin/registrations/ANM-2026-0F99", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        with patch("app.routes.admin.send_approval_email") as mock_email:
            response = await client.post("/admin/registrations/ANM-2026-0F99/approve", data={
                "csrf_token": csrf,
            }, cookies=admin_cookie())

        assert response.status_code == 200
        assert "Cannot approve" in response.text
        assert "0 remaining" in response.text
        mock_email.assert_not_called()

    reg = db.query(Registration).filter(Registration.registration_id == "ANM-2026-0F99").first()
    db.refresh(reg)
    assert reg.status == "pending"  # Unchanged


# ========================================
# Reject
# ========================================

@pytest.mark.anyio
async def test_reject_registration_with_reason(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, status="pending", reg_id="ANM-2026-0030")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        detail = await client.get("/admin/registrations/ANM-2026-0030", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        with patch("app.routes.admin.send_rejection_email", return_value=True) as mock_email:
            response = await client.post("/admin/registrations/ANM-2026-0030/reject", data={
                "csrf_token": csrf,
                "rejection_reason": "Does not meet food safety requirements",
            }, cookies=admin_cookie())

        assert response.status_code == 303
        mock_email.assert_called_once_with(
            "vendor@test.com", "ANM-2026-0030", "Does not meet food safety requirements"
        )

    reg = db.query(Registration).filter(Registration.registration_id == "ANM-2026-0030").first()
    db.refresh(reg)
    assert reg.status == "rejected"
    assert reg.rejected_at is not None
    assert reg.rejection_reason == "Does not meet food safety requirements"


@pytest.mark.anyio
async def test_reject_registration_without_reason(db):
    """Empty rejection reason is blocked with an error message."""
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, status="pending", reg_id="ANM-2026-0031")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        detail = await client.get("/admin/registrations/ANM-2026-0031", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        with patch("app.routes.admin.send_rejection_email", return_value=True) as mock_email:
            response = await client.post("/admin/registrations/ANM-2026-0031/reject", data={
                "csrf_token": csrf,
                "rejection_reason": "",
            }, cookies=admin_cookie())

        assert response.status_code == 200
        assert "rejection reason is required" in response.text.lower()
        mock_email.assert_not_called()

    reg = db.query(Registration).filter(Registration.registration_id == "ANM-2026-0031").first()
    db.refresh(reg)
    assert reg.status == "pending"


@pytest.mark.anyio
async def test_revoke_approved_registration(db):
    """Admin can revoke an approval, returning to pending."""
    seed_admin(db)
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="approved", reg_id="ANM-2026-0032")
    reg.approved_at = datetime.now(timezone.utc)
    db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        detail = await client.get("/admin/registrations/ANM-2026-0032", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        response = await client.post("/admin/registrations/ANM-2026-0032/unreject", data={
            "csrf_token": csrf,
        }, cookies=admin_cookie())

        assert response.status_code == 303

    reg = db.query(Registration).filter(Registration.registration_id == "ANM-2026-0032").first()
    db.refresh(reg)
    assert reg.status == "pending"
    assert reg.approved_at is None


@pytest.mark.anyio
async def test_revoke_rejected_registration(db):
    """Admin can revoke a rejection, returning to pending."""
    seed_admin(db)
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="rejected", reg_id="ANM-2026-0033")
    reg.rejected_at = datetime.now(timezone.utc)
    reg.rejection_reason = "Test reason"
    db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        detail = await client.get("/admin/registrations/ANM-2026-0033", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        response = await client.post("/admin/registrations/ANM-2026-0033/unreject", data={
            "csrf_token": csrf,
        }, cookies=admin_cookie())

        assert response.status_code == 303

    reg = db.query(Registration).filter(Registration.registration_id == "ANM-2026-0033").first()
    db.refresh(reg)
    assert reg.status == "pending"
    assert reg.rejected_at is None
    assert reg.rejection_reason is None


# ========================================
# Update fields (category)
# ========================================

@pytest.mark.anyio
async def test_update_category(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, reg_id="ANM-2026-0041")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        detail = await client.get("/admin/registrations/ANM-2026-0041", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        response = await client.post("/admin/registrations/ANM-2026-0041/update", data={
            "csrf_token": csrf,
            "category": "merchandise",
        }, cookies=admin_cookie())

        assert response.status_code == 303

    reg = db.query(Registration).filter(Registration.registration_id == "ANM-2026-0041").first()
    db.refresh(reg)
    assert reg.category == "merchandise"


# ========================================
# Inventory
# ========================================

@pytest.mark.anyio
async def test_inventory_page_loads(db):
    seed_admin(db)
    seed_booth_types(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/inventory", cookies=admin_cookie())
        assert response.status_code == 200
        assert "Premium" in response.text
        assert "Regular" in response.text


@pytest.mark.anyio
async def test_update_inventory_quantity(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    original_qty = booths[0].total_quantity

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        inv_page = await client.get("/admin/inventory", cookies=admin_cookie())
        csrf = extract_csrf(inv_page.text)

        response = await client.post(f"/admin/inventory/{booths[0].id}", data={
            "csrf_token": csrf,
            "total_quantity": "25",
            "price": "150.00",
            "description": "Updated description",
        }, cookies=admin_cookie())

        assert response.status_code == 303

    bt = db.query(BoothType).filter(BoothType.id == booths[0].id).first()
    db.refresh(bt)
    assert bt.total_quantity == 25


@pytest.mark.anyio
async def test_inventory_reflects_approved_registrations(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, status="approved", reg_id="ANM-2026-0060", email="a@t.com")
    make_registration(db, booths[0].id, status="paid", reg_id="ANM-2026-0061", email="b@t.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/inventory", cookies=admin_cookie())
        assert response.status_code == 200
        # Premium booth: 20 total, 1 approved, 1 paid = 18 available of 20
        assert "18 available of 20" in response.text


# ========================================
# Settings
# ========================================

@pytest.mark.anyio
async def test_settings_page_loads(db):
    seed_admin(db)
    seed_event(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/settings", cookies=admin_cookie())
        assert response.status_code == 200
        assert "Registration Opens" in response.text


@pytest.mark.anyio
async def test_update_settings_dates(db):
    seed_admin(db)
    seed_event(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        page = await client.get("/admin/settings", cookies=admin_cookie())
        csrf = extract_csrf(page.text)

        response = await client.post("/admin/settings", data={
            "csrf_token": csrf,
            "event_name": "Test Event",
            "event_start_date": "2026-09-26",
            "event_end_date": "2026-09-27",
            "registration_open_date": "2026-05-01T00:00",
            "registration_close_date": "2026-10-01T23:59",
        }, cookies=admin_cookie())

        assert response.status_code == 303

    settings = db.query(EventSettings).first()
    db.refresh(settings)
    assert settings.registration_open_date.month == 5
    assert settings.registration_close_date.month == 10


# ========================================
# CSV Export
# ========================================

@pytest.mark.anyio
async def test_csv_export_headers_and_data(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, status="pending", reg_id="ANM-2026-0070",
                      business_name="Export Biz")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/export", cookies=admin_cookie())
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/csv; charset=utf-8"
        assert "attachment" in response.headers.get("content-disposition", "")

        body = response.text
        lines = body.strip().split("\n")
        assert len(lines) == 2  # Header + 1 data row
        assert "Registration ID" in lines[0]
        assert "ANM-2026-0070" in lines[1]
        assert "Export Biz" in lines[1]


@pytest.mark.anyio
async def test_csv_export_empty(db):
    seed_admin(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/export", cookies=admin_cookie())
        assert response.status_code == 200
        lines = response.text.strip().split("\n")
        assert len(lines) == 1  # Header only


@pytest.mark.anyio
async def test_csv_export_requires_admin(db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get("/admin/export")
        assert response.status_code == 303
        assert "/auth/login" in response.headers["location"]
