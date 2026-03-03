"""Tests for payment deadline feature: deadline computation, approval, transitions, urgency, reminders, settings."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import BoothType, EventSettings, Registration
from app.services.registration import (
    compute_payment_deadline,
    get_unpaid_registrations,
    approve_with_inventory_check,
    transition_status,
)
from tests.helpers import (
    admin_cookie, extract_csrf, seed_admin, seed_booth_types,
    seed_event, seed_event_open, make_registration,
)


# --- Helpers ---

def _seed_settings(db, **overrides):
    """Seed event settings with payment deadline defaults."""
    settings = db.query(EventSettings).first()
    if not settings:
        kwargs = dict(
            id=1,
            event_name="Test Event",
            event_start_date=datetime(2026, 10, 17).date(),
            event_end_date=datetime(2026, 10, 18).date(),
            registration_open_date=datetime(2020, 1, 1),
            registration_close_date=datetime(2030, 12, 31, 23, 59, 59),
            vendor_agreement_text="Agreement text.",
            payment_deadline_days=7,
            reminder_1_days=2,
            reminder_2_days=5,
            reminder_1_subject="Reminder 1 — {event_name}",
            reminder_1_body="<p>Reminder 1 body for {registration_id}</p>",
            reminder_2_subject="Reminder 2 — {event_name}",
            reminder_2_body="<p>Reminder 2 body for {registration_id}</p>",
        )
        kwargs.update(overrides)
        settings = EventSettings(**kwargs)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def _make_booth(db, qty=10):
    bt = BoothType(name="Regular", description="Test", total_quantity=qty, price=10000, sort_order=1)
    db.add(bt)
    db.commit()
    db.refresh(bt)
    return bt


def _make_reg(db, booth_type_id, status="pending", email="vendor@test.com",
              reg_id="ANM-2026-0001", approved_at=None, payment_deadline=None,
              last_reminder_sent_at=None, reminder_count=0):
    reg = Registration(
        registration_id=reg_id,
        email=email,
        business_name="Test Biz",
        contact_name="Test Vendor",
        phone="555-0100",
        category="food",
        description="Test food booth",
        booth_type_id=booth_type_id,
        status=status,
        agreement_accepted_at=datetime.now(timezone.utc),
        agreement_ip_address="127.0.0.1",
        approved_at=approved_at,
        payment_deadline=payment_deadline,
        last_reminder_sent_at=last_reminder_sent_at,
        reminder_count=reminder_count,
    )
    db.add(reg)
    db.commit()
    db.refresh(reg)
    return reg


# ========================================
# compute_payment_deadline
# ========================================

def test_compute_deadline_correct_end_of_day():
    approved = datetime(2026, 3, 1, 10, 30, 0, tzinfo=timezone.utc)
    result = compute_payment_deadline(approved, 7)
    assert result == datetime(2026, 3, 8, 23, 59, 59, tzinfo=timezone.utc)


def test_compute_deadline_1_day():
    approved = datetime(2026, 6, 15, 8, 0, 0, tzinfo=timezone.utc)
    result = compute_payment_deadline(approved, 1)
    assert result == datetime(2026, 6, 16, 23, 59, 59, tzinfo=timezone.utc)


def test_compute_deadline_30_days():
    approved = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    result = compute_payment_deadline(approved, 30)
    assert result == datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc)


# ========================================
# approve_with_inventory_check sets deadline
# ========================================

def test_approve_sets_payment_deadline(db):
    bt = _make_booth(db)
    settings = _seed_settings(db, payment_deadline_days=7)
    reg = _make_reg(db, bt.id, status="pending")

    result = approve_with_inventory_check(db, reg)
    assert result.status == "approved"
    assert result.payment_deadline is not None
    # Deadline should be 7 days after approval, end of day
    expected_day = (result.approved_at + timedelta(days=7)).date()
    assert result.payment_deadline.date() == expected_day
    assert result.payment_deadline.hour == 23
    assert result.payment_deadline.minute == 59
    assert result.payment_deadline.second == 59
    assert result.reminder_count == 0
    assert result.last_reminder_sent_at is None


# ========================================
# Transitions clear deadline fields
# ========================================

def test_approved_to_pending_clears_deadline(db):
    bt = _make_booth(db)
    now = datetime.now(timezone.utc)
    reg = _make_reg(db, bt.id, status="approved",
                    approved_at=now,
                    payment_deadline=now + timedelta(days=7),
                    last_reminder_sent_at=now,
                    reminder_count=2)
    reg.approved_price = 10000
    db.commit()

    result = transition_status(db, reg, "pending", reversal_reason="Test")
    assert result.payment_deadline is None
    assert result.last_reminder_sent_at is None
    assert result.reminder_count == 0


def test_approved_to_rejected_clears_deadline(db):
    bt = _make_booth(db)
    now = datetime.now(timezone.utc)
    reg = _make_reg(db, bt.id, status="approved",
                    approved_at=now,
                    payment_deadline=now + timedelta(days=7),
                    reminder_count=1)
    reg.approved_price = 10000
    db.commit()

    result = transition_status(db, reg, "rejected", reversal_reason="Test")
    assert result.payment_deadline is None
    assert result.last_reminder_sent_at is None
    assert result.reminder_count == 0


def test_approved_to_withdrawn_clears_deadline(db):
    bt = _make_booth(db)
    now = datetime.now(timezone.utc)
    reg = _make_reg(db, bt.id, status="approved",
                    approved_at=now,
                    payment_deadline=now + timedelta(days=7))
    reg.approved_price = 10000
    db.commit()

    result = transition_status(db, reg, "withdrawn")
    assert result.payment_deadline is None


# ========================================
# get_unpaid_registrations
# ========================================

def test_unpaid_registrations_urgency_levels(db):
    bt = _make_booth(db)
    settings = _seed_settings(db, payment_deadline_days=7, reminder_1_days=2, reminder_2_days=5)
    now = datetime.now(timezone.utc)

    # Normal: approved 1 day ago
    r1 = _make_reg(db, bt.id, status="approved", reg_id="ANM-2026-0001",
                   email="a@test.com",
                   approved_at=now - timedelta(days=1),
                   payment_deadline=now + timedelta(days=6))

    # Reminder 1: approved 3 days ago (past R1=2, before R2=5)
    r2 = _make_reg(db, bt.id, status="approved", reg_id="ANM-2026-0002",
                   email="b@test.com",
                   approved_at=now - timedelta(days=3),
                   payment_deadline=now + timedelta(days=4))

    # Reminder 2: approved 6 days ago (past R2=5, before deadline=7)
    r3 = _make_reg(db, bt.id, status="approved", reg_id="ANM-2026-0003",
                   email="c@test.com",
                   approved_at=now - timedelta(days=6),
                   payment_deadline=now + timedelta(days=1))

    # Overdue: approved 8 days ago (past deadline=7)
    r4 = _make_reg(db, bt.id, status="approved", reg_id="ANM-2026-0004",
                   email="d@test.com",
                   approved_at=now - timedelta(days=8),
                   payment_deadline=now - timedelta(days=1))

    result = get_unpaid_registrations(db, settings)
    assert len(result) == 4

    urgencies = {r["registration"].registration_id: r["urgency"] for r in result}
    assert urgencies["ANM-2026-0001"] == "normal"
    assert urgencies["ANM-2026-0002"] == "reminder_1"
    assert urgencies["ANM-2026-0003"] == "reminder_2"
    assert urgencies["ANM-2026-0004"] == "overdue"

    # Sorted by payment_deadline ascending (most urgent first)
    assert result[0]["registration"].registration_id == "ANM-2026-0004"


def test_unpaid_registrations_excludes_non_approved(db):
    bt = _make_booth(db)
    settings = _seed_settings(db)
    _make_reg(db, bt.id, status="pending", reg_id="ANM-2026-0001")
    _make_reg(db, bt.id, status="paid", reg_id="ANM-2026-0002", email="b@test.com")

    result = get_unpaid_registrations(db, settings)
    assert len(result) == 0


# ========================================
# EventSettings.validate_reminder_days
# ========================================

def test_validate_reminder_days_valid(db):
    settings = _seed_settings(db, payment_deadline_days=7, reminder_1_days=2, reminder_2_days=5)
    errors = settings.validate_reminder_days()
    assert errors == []


def test_validate_reminder_days_r1_gte_r2(db):
    settings = _seed_settings(db, payment_deadline_days=7, reminder_1_days=5, reminder_2_days=3)
    errors = settings.validate_reminder_days()
    assert len(errors) > 0
    assert any("earlier" in e.lower() for e in errors)


def test_validate_reminder_days_r2_gte_deadline(db):
    settings = _seed_settings(db, payment_deadline_days=5, reminder_1_days=2, reminder_2_days=5)
    errors = settings.validate_reminder_days()
    assert len(errors) > 0
    assert any("before" in e.lower() for e in errors)


def test_validate_reminder_days_r1_zero():
    settings = EventSettings(
        id=1,
        event_name="Test", event_start_date=datetime(2026, 10, 17).date(),
        event_end_date=datetime(2026, 10, 18).date(),
        registration_open_date=datetime(2020, 1, 1),
        registration_close_date=datetime(2030, 12, 31),
        vendor_agreement_text="Test",
        payment_deadline_days=7, reminder_1_days=0, reminder_2_days=3,
    )
    errors = settings.validate_reminder_days()
    assert any("at least 1" in e.lower() for e in errors)


# ========================================
# EventSettings.derive_reminder_defaults
# ========================================

def test_derive_defaults_7_days():
    r1, r2 = EventSettings.derive_reminder_defaults(7)
    assert 1 <= r1 < r2 < 7


def test_derive_defaults_3_days():
    r1, r2 = EventSettings.derive_reminder_defaults(3)
    assert r1 == 1
    assert r2 == 2


def test_derive_defaults_2_days():
    r1, r2 = EventSettings.derive_reminder_defaults(2)
    assert r1 == 1
    assert r2 == 1


def test_derive_defaults_14_days():
    r1, r2 = EventSettings.derive_reminder_defaults(14)
    assert 1 <= r1 < r2 < 14


# ========================================
# POST /admin/registrations/{id}/remind
# ========================================

@pytest.mark.anyio
async def test_remind_sends_email_and_updates_tracking(db):
    seed_admin(db)
    bt = _make_booth(db)
    _seed_settings(db)
    now = datetime.now(timezone.utc)
    reg = _make_reg(db, bt.id, status="approved", reg_id="ANM-2026-0001",
                    approved_at=now - timedelta(days=3),
                    payment_deadline=now + timedelta(days=4))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detail = await client.get("/admin/registrations/ANM-2026-0001", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        with patch("app.routes.admin.send_payment_reminder_email") as mock_send:
            resp = await client.post(
                "/admin/registrations/ANM-2026-0001/remind",
                data={"csrf_token": csrf},
                cookies=admin_cookie(),
                follow_redirects=False,
            )
        assert resp.status_code == 303

    db.refresh(reg)
    assert reg.reminder_count == 1
    assert reg.last_reminder_sent_at is not None


@pytest.mark.anyio
async def test_remind_rate_limits_within_hour(db):
    seed_admin(db)
    bt = _make_booth(db)
    _seed_settings(db)
    now = datetime.now(timezone.utc)
    reg = _make_reg(db, bt.id, status="approved", reg_id="ANM-2026-0001",
                    approved_at=now - timedelta(days=3),
                    payment_deadline=now + timedelta(days=4),
                    last_reminder_sent_at=now - timedelta(minutes=30),
                    reminder_count=1)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detail = await client.get("/admin/registrations/ANM-2026-0001", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        with patch("app.routes.admin.send_payment_reminder_email") as mock_send:
            resp = await client.post(
                "/admin/registrations/ANM-2026-0001/remind",
                data={"csrf_token": csrf},
                cookies=admin_cookie(),
            )
        assert resp.status_code == 200  # re-rendered detail page with error
        assert "less than 1 hour" in resp.text
        mock_send.assert_not_called()

    db.refresh(reg)
    assert reg.reminder_count == 1  # unchanged


@pytest.mark.anyio
async def test_remind_rejects_non_approved(db):
    seed_admin(db)
    bt = _make_booth(db)
    _seed_settings(db)
    reg = _make_reg(db, bt.id, status="pending", reg_id="ANM-2026-0001")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detail = await client.get("/admin/registrations/ANM-2026-0001", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        with patch("app.routes.admin.send_payment_reminder_email") as mock_send:
            resp = await client.post(
                "/admin/registrations/ANM-2026-0001/remind",
                data={"csrf_token": csrf},
                cookies=admin_cookie(),
            )
        assert resp.status_code == 200
        assert "approved" in resp.text.lower()
        mock_send.assert_not_called()


# ========================================
# Settings POST validates reminder constraints
# ========================================

@pytest.mark.anyio
async def test_settings_validates_reminder_days(db):
    seed_admin(db)
    _seed_settings(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/admin/settings", cookies=admin_cookie())
        csrf = extract_csrf(page.text)

        settings = db.query(EventSettings).first()
        resp = await client.post(
            "/admin/settings",
            data={
                "csrf_token": csrf,
                "event_name": settings.event_name,
                "event_start_date": "2026-10-17",
                "event_end_date": "2026-10-18",
                "registration_open_date": "2020-01-01T00:00",
                "registration_close_date": "2030-12-31T23:59",
                "vendor_agreement_text": "Test",
                "payment_deadline_days": "5",
                "reminder_1_days": "5",   # R1 >= R2 — invalid
                "reminder_2_days": "3",
            },
            cookies=admin_cookie(),
        )
        # Should re-render settings with error (not redirect)
        assert resp.status_code == 200
        assert "earlier" in resp.text.lower() or "before" in resp.text.lower()


# ========================================
# Approval email includes deadline_date
# ========================================

@pytest.mark.anyio
async def test_approval_email_includes_deadline(db):
    seed_admin(db)
    bt = _make_booth(db)
    _seed_settings(db, payment_deadline_days=7)
    reg = _make_reg(db, bt.id, status="pending", reg_id="ANM-2026-0001")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detail = await client.get("/admin/registrations/ANM-2026-0001", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        with patch("app.routes.admin.send_approval_email") as mock_send:
            resp = await client.post(
                "/admin/registrations/ANM-2026-0001/approve",
                data={"csrf_token": csrf},
                cookies=admin_cookie(),
                follow_redirects=False,
            )
        assert resp.status_code == 303
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        # deadline_date should be a non-None string
        assert call_kwargs.kwargs.get("deadline_date") is not None or \
               (len(call_kwargs.args) > 4 and call_kwargs.args[4] is not None)
