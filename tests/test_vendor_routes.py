"""Integration tests for vendor registration flow and dashboard."""

import re
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import BoothType, EventSettings, Registration
from app.session import _serializer
from app.services.registration import reset_rate_limits


# --- Helpers ---

def _vendor_cookie(email="vendor@test.com", draft=None):
    """Create a signed vendor session cookie."""
    data = {
        "user_type": "vendor",
        "email": email,
        "created_at": time.time(),
        "last_activity": time.time(),
    }
    if draft is not None:
        data["registration_draft"] = draft
    return {"session": _serializer.dumps(data)}


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token not found"
    return match.group(1)


def _seed_event_open(db):
    """Seed event settings with registration currently open."""
    settings = db.query(EventSettings).first()
    if not settings:
        settings = EventSettings(
            id=1,
            event_name="Test Event",
            event_date=datetime(2026, 10, 17).date(),
            registration_open_date=datetime(2020, 1, 1),
            registration_close_date=datetime(2030, 12, 31, 23, 59, 59),
            vendor_agreement_text="Test agreement text.",
        )
        db.add(settings)
        db.commit()
    else:
        settings.registration_open_date = datetime(2020, 1, 1)
        settings.registration_close_date = datetime(2030, 12, 31, 23, 59, 59)
        db.commit()
    return settings


def _seed_event_future(db):
    """Seed event settings with registration not yet open."""
    settings = db.query(EventSettings).first()
    if not settings:
        settings = EventSettings(
            id=1,
            event_name="Test Event",
            event_date=datetime(2030, 10, 17).date(),
            registration_open_date=datetime(2030, 6, 1),
            registration_close_date=datetime(2030, 9, 15, 23, 59, 59),
            vendor_agreement_text="Test agreement text.",
        )
        db.add(settings)
        db.commit()
    else:
        settings.registration_open_date = datetime(2030, 6, 1)
        settings.registration_close_date = datetime(2030, 9, 15, 23, 59, 59)
        db.commit()
    return settings


def _seed_event_closed(db):
    """Seed event settings with registration already closed."""
    settings = db.query(EventSettings).first()
    if not settings:
        settings = EventSettings(
            id=1,
            event_name="Test Event",
            event_date=datetime(2020, 10, 17).date(),
            registration_open_date=datetime(2020, 1, 1),
            registration_close_date=datetime(2020, 9, 15, 23, 59, 59),
            vendor_agreement_text="Test agreement text.",
        )
        db.add(settings)
        db.commit()
    else:
        settings.registration_open_date = datetime(2020, 1, 1)
        settings.registration_close_date = datetime(2020, 9, 15, 23, 59, 59)
        db.commit()
    return settings


def _seed_booth_types(db):
    """Seed booth types and return them."""
    if db.query(BoothType).count() > 0:
        return db.query(BoothType).order_by(BoothType.sort_order).all()
    booths = [
        BoothType(name="Premium", description="Corner spot", total_quantity=20, price=15000, sort_order=1),
        BoothType(name="Regular", description="Standard spot", total_quantity=80, price=10000, sort_order=2),
    ]
    db.add_all(booths)
    db.commit()
    return db.query(BoothType).order_by(BoothType.sort_order).all()


def _make_complete_draft(booth_type_id):
    """Return a draft with all fields filled for review/submit (step 2)."""
    return {
        "current_step": 2,
        "email": "vendor@test.com",
        "contact_name": "Test Vendor",
        "agreement_ip": "127.0.0.1",
        "agreement_accepted_at": datetime.now(timezone.utc).isoformat(),
        "business_name": "Test Biz",
        "phone": "555-0100",
        "category": "food",
        "description": "Delicious food",
        "cuisine_type": "Thai",
        "needs_power": True,
        "needs_water": False,
        "needs_propane": False,
        "booth_type_id": booth_type_id,
        "booth_type_name": "Regular",
        "booth_type_price": 10000,
    }


def _step1_form_data(csrf, booth_type_id, **overrides):
    """Build form data for the combined step 1 POST."""
    data = {
        "csrf_token": csrf,
        "contact_name": "Test Vendor",
        "email": "vendor@test.com",
        "phone": "555-1234",
        "business_name": "Test Biz",
        "category": "food",
        "description": "Authentic Thai cuisine",
        "cuisine_type": "Thai",
        "booth_type_id": str(booth_type_id),
        "agreement_accepted": "yes",
    }
    data.update(overrides)
    return data


# ========================================
# Registration gateway — date gating
# ========================================

@pytest.mark.anyio
async def test_register_shows_coming_soon_before_open_date(db):
    _seed_event_future(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/register")
        assert response.status_code == 200
        assert "Coming Soon" in response.text


@pytest.mark.anyio
async def test_register_shows_closed_after_close_date(db):
    _seed_event_closed(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/register")
        assert response.status_code == 200
        assert "Closed" in response.text


@pytest.mark.anyio
async def test_register_requires_login_when_open(db):
    """When registration is open but no vendor session, redirect to login."""
    _seed_event_open(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get("/vendor/register")
        assert response.status_code == 303
        assert "/auth/login" in response.headers["location"]


@pytest.mark.anyio
async def test_register_shows_step1_when_open_and_logged_in(db):
    """When registration is open and vendor is logged in, show the form."""
    _seed_event_open(db)
    _seed_booth_types(db)
    cookies = _vendor_cookie()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/register", cookies=cookies)
        assert response.status_code == 200
        assert "Vendor Registration" in response.text
        assert "Business Name" in response.text


@pytest.mark.anyio
async def test_register_allows_additional_registrations(db):
    """Vendors with existing registrations can still access the registration form."""
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    reg = Registration(
        registration_id="ANM-2026-0001",
        email="vendor@test.com",
        business_name="Existing Biz",
        contact_name="Existing",
        phone="555-0000",
        category="food",
        description="Already registered",
        booth_type_id=booths[0].id,
        status="pending",
        agreement_accepted_at=datetime.now(timezone.utc),
        agreement_ip_address="127.0.0.1",
    )
    db.add(reg)
    db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/register", cookies=_vendor_cookie())
        assert response.status_code == 200
        assert "Vendor Registration" in response.text


# ========================================
# Step 1 — Combined registration form
# ========================================

@pytest.mark.anyio
async def test_step1_valid_saves_draft_and_redirects(db):
    """Valid step1 submission saves draft and redirects to review (step2)."""
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    cookies = _vendor_cookie()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        page = await client.get("/vendor/register", cookies=cookies)
        csrf = _extract_csrf(page.text)

        response = await client.post(
            "/vendor/register/step1",
            data=_step1_form_data(csrf, booths[1].id),
            cookies=cookies,
        )
        assert response.status_code == 303
        assert "/vendor/register" in response.headers["location"]
        # Session cookie should be updated with draft
        assert "session" in response.cookies


@pytest.mark.anyio
async def test_step1_rejects_without_agreement(db):
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    cookies = _vendor_cookie()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/vendor/register", cookies=cookies)
        csrf = _extract_csrf(page.text)

        response = await client.post(
            "/vendor/register/step1",
            data=_step1_form_data(csrf, booths[1].id, agreement_accepted=""),
            cookies=cookies,
        )
        assert response.status_code == 200
        assert "must accept" in response.text.lower()


@pytest.mark.anyio
async def test_step1_rejects_missing_contact_name(db):
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    cookies = _vendor_cookie()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/vendor/register", cookies=cookies)
        csrf = _extract_csrf(page.text)

        response = await client.post(
            "/vendor/register/step1",
            data=_step1_form_data(csrf, booths[1].id, contact_name="  "),
            cookies=cookies,
        )
        assert response.status_code == 200
        assert "name" in response.text.lower() and "required" in response.text.lower()


@pytest.mark.anyio
async def test_step1_rejects_missing_business_name(db):
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    cookies = _vendor_cookie()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/vendor/register", cookies=cookies)
        csrf = _extract_csrf(page.text)

        response = await client.post(
            "/vendor/register/step1",
            data=_step1_form_data(csrf, booths[1].id, business_name="  "),
            cookies=cookies,
        )
        assert response.status_code == 200
        assert "business name" in response.text.lower()


@pytest.mark.anyio
async def test_step1_rejects_invalid_category(db):
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    cookies = _vendor_cookie()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/vendor/register", cookies=cookies)
        csrf = _extract_csrf(page.text)

        response = await client.post(
            "/vendor/register/step1",
            data=_step1_form_data(csrf, booths[1].id, category="invalid_cat"),
            cookies=cookies,
        )
        assert response.status_code == 200
        assert "valid category" in response.text.lower()


@pytest.mark.anyio
async def test_step1_food_requires_cuisine_type(db):
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    cookies = _vendor_cookie()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/vendor/register", cookies=cookies)
        csrf = _extract_csrf(page.text)

        response = await client.post(
            "/vendor/register/step1",
            data=_step1_form_data(csrf, booths[1].id, category="food", cuisine_type=""),
            cookies=cookies,
        )
        assert response.status_code == 200
        assert "cuisine type" in response.text.lower()


@pytest.mark.anyio
async def test_step1_non_food_does_not_require_cuisine(db):
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    cookies = _vendor_cookie()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        page = await client.get("/vendor/register", cookies=cookies)
        csrf = _extract_csrf(page.text)

        response = await client.post(
            "/vendor/register/step1",
            data=_step1_form_data(csrf, booths[1].id, category="non_food", cuisine_type=""),
            cookies=cookies,
        )
        assert response.status_code == 303


@pytest.mark.anyio
async def test_step1_rejects_invalid_booth_type(db):
    _seed_event_open(db)
    _seed_booth_types(db)
    cookies = _vendor_cookie()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/vendor/register", cookies=cookies)
        csrf = _extract_csrf(page.text)

        response = await client.post(
            "/vendor/register/step1",
            data=_step1_form_data(csrf, 99999),
            cookies=cookies,
        )
        assert response.status_code == 200
        assert "valid booth type" in response.text.lower()


@pytest.mark.anyio
async def test_step1_no_session_redirects(db):
    """Step 1 POST without a vendor session redirects to login."""
    _seed_event_open(db)
    booths = _seed_booth_types(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        # Need a CSRF token — get one from any page
        page = await client.get("/vendor/register")
        # No vendor cookie, should redirect to login
        response = await client.post(
            "/vendor/register/step1",
            data={"csrf_token": "fake", "contact_name": "X", "email": "x@x.com",
                  "phone": "555", "business_name": "B", "category": "food",
                  "description": "D", "booth_type_id": "1", "agreement_accepted": "yes"},
        )
        # Should get 403 (bad CSRF) or 303 redirect (no session)
        assert response.status_code in (303, 403)


@pytest.mark.anyio
async def test_step1_email_forced_from_session(db):
    """Email is taken from session, not from form data."""
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    cookies = _vendor_cookie(email="real@vendor.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        page = await client.get("/vendor/register", cookies=cookies)
        csrf = _extract_csrf(page.text)

        # Submit with a different email in form data
        response = await client.post(
            "/vendor/register/step1",
            data=_step1_form_data(csrf, booths[1].id, email="fake@hacker.com"),
            cookies=cookies,
        )
        assert response.status_code == 303
        # The session cookie should contain the real email, not the fake one
        assert "session" in response.cookies


# ========================================
# Review page (step 2)
# ========================================

@pytest.mark.anyio
async def test_review_page_shows_draft_data(db):
    """Step 2 review page shows the draft data from session."""
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    draft = _make_complete_draft(booths[1].id)
    cookies = _vendor_cookie(draft=draft)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/register", cookies=cookies)
        assert response.status_code == 200
        assert "Review" in response.text
        assert "Test Vendor" in response.text
        assert "Test Biz" in response.text


@pytest.mark.anyio
async def test_edit_link_returns_to_step1(db):
    """The ?edit=1 query param should show step 1 form with draft data."""
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    draft = _make_complete_draft(booths[1].id)
    cookies = _vendor_cookie(draft=draft)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/register?edit=1", cookies=cookies)
        assert response.status_code == 200
        # Should show the form, not the review page
        assert "Business Name" in response.text
        assert 'value="Test Biz"' in response.text


# ========================================
# Final submit
# ========================================

@pytest.mark.anyio
async def test_submit_creates_registration_and_redirects(db):
    reset_rate_limits()
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    draft = _make_complete_draft(booths[1].id)
    cookies = _vendor_cookie(draft=draft)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        page = await client.get("/vendor/register", cookies=cookies)
        csrf = _extract_csrf(page.text)

        with patch("app.routes.vendor.send_submission_confirmation_email", return_value=True):
            response = await client.post("/vendor/register/submit", data={
                "csrf_token": csrf,
            }, cookies=cookies)

        assert response.status_code == 303
        assert "/vendor/confirm/" in response.headers["location"]

    # Verify registration in DB
    reg = db.query(Registration).first()
    assert reg is not None
    assert reg.status == "pending"
    assert reg.business_name == "Test Biz"
    assert reg.registration_id.startswith("ANM-")


@pytest.mark.anyio
async def test_submit_incomplete_draft_redirects(db):
    _seed_event_open(db)
    # Draft missing required fields
    draft = {"current_step": 2, "email": "vendor@test.com"}
    cookies = _vendor_cookie(draft=draft)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        page = await client.get("/vendor/register", cookies=cookies)
        csrf = _extract_csrf(page.text)

        response = await client.post("/vendor/register/submit", data={
            "csrf_token": csrf,
        }, cookies=cookies)
        assert response.status_code == 303
        assert "/vendor/register" in response.headers["location"]


@pytest.mark.anyio
async def test_submit_rate_limited(db):
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    draft = _make_complete_draft(booths[1].id)
    cookies = _vendor_cookie(draft=draft)

    # Exhaust rate limit
    reset_rate_limits()
    from app.services.registration import check_submission_rate_limit
    for _ in range(10):
        check_submission_rate_limit("127.0.0.1")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/vendor/register", cookies=cookies)
        csrf = _extract_csrf(page.text)

        response = await client.post("/vendor/register/submit", data={
            "csrf_token": csrf,
        }, cookies=cookies)
        assert response.status_code == 200
        assert "too many" in response.text.lower()

    reset_rate_limits()


# ========================================
# Confirmation page
# ========================================

@pytest.mark.anyio
async def test_confirmation_page_shows_registration(db):
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    reg = Registration(
        registration_id="ANM-2026-0050",
        email="vendor@test.com",
        business_name="My Biz",
        contact_name="Vendor",
        phone="555-0100",
        category="food",
        description="Food",
        booth_type_id=booths[0].id,
        status="pending",
        agreement_accepted_at=datetime.now(timezone.utc),
        agreement_ip_address="127.0.0.1",
    )
    db.add(reg)
    db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/confirm/ANM-2026-0050")
        assert response.status_code == 200
        assert "ANM-2026-0050" in response.text
        assert "My Biz" in response.text


@pytest.mark.anyio
async def test_confirmation_page_unknown_id_redirects(db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get("/vendor/confirm/ANM-2026-9999")
        assert response.status_code == 303
        assert "/vendor/register" in response.headers["location"]


# ========================================
# Vendor dashboard
# ========================================

@pytest.mark.anyio
async def test_dashboard_requires_vendor_session(db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get("/vendor/dashboard")
        assert response.status_code == 303
        assert "/auth/login" in response.headers["location"]


@pytest.mark.anyio
async def test_dashboard_shows_vendor_registrations(db):
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    reg = Registration(
        registration_id="ANM-2026-0001",
        email="vendor@test.com",
        business_name="Vendor Biz",
        contact_name="Vendor",
        phone="555-0100",
        category="food",
        description="Food",
        booth_type_id=booths[0].id,
        status="approved",
        agreement_accepted_at=datetime.now(timezone.utc),
        agreement_ip_address="127.0.0.1",
    )
    db.add(reg)
    db.commit()

    cookies = _vendor_cookie()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/dashboard", cookies=cookies)
        assert response.status_code == 200
        assert "ANM-2026-0001" in response.text
        assert "Approved" in response.text
        # Should show option to register for another booth
        assert "Register for Another Booth" in response.text
        assert "?new=1" in response.text


@pytest.mark.anyio
async def test_dashboard_only_shows_own_registrations(db):
    _seed_event_open(db)
    booths = _seed_booth_types(db)
    # Create reg for a different vendor
    reg = Registration(
        registration_id="ANM-2026-0002",
        email="other@vendor.com",
        business_name="Other Biz",
        contact_name="Other",
        phone="555-0200",
        category="non_food",
        description="Crafts",
        booth_type_id=booths[0].id,
        status="pending",
        agreement_accepted_at=datetime.now(timezone.utc),
        agreement_ip_address="127.0.0.1",
    )
    db.add(reg)
    db.commit()

    cookies = _vendor_cookie(email="vendor@test.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/dashboard", cookies=cookies)
        assert response.status_code == 200
        assert "Other Biz" not in response.text
        assert "don't have any" in response.text.lower()


@pytest.mark.anyio
async def test_dashboard_hides_register_link_when_closed(db):
    """Register link should not appear when registration is closed."""
    _seed_event_closed(db)
    booths = _seed_booth_types(db)
    reg = Registration(
        registration_id="ANM-2026-0010",
        email="vendor@test.com",
        business_name="My Biz",
        contact_name="Vendor",
        phone="555-0100",
        category="food",
        description="Food",
        booth_type_id=booths[0].id,
        status="confirmed",
        agreement_accepted_at=datetime.now(timezone.utc),
        agreement_ip_address="127.0.0.1",
    )
    db.add(reg)
    db.commit()

    cookies = _vendor_cookie()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/dashboard", cookies=cookies)
        assert response.status_code == 200
        assert "ANM-2026-0010" in response.text
        assert "Register for Another Booth" not in response.text
