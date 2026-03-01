"""Shared test helpers for session cookies, seeding, and registration creation."""

import json
import re
import time
from datetime import datetime, timezone
from unittest.mock import patch

from app.models import AdminUser, BoothType, EventSettings, InsuranceDocument, Registration, RegistrationDraft
from app.session import _serializer


def admin_cookie(email="admin@test.com"):
    """Create a signed admin session cookie."""
    data = {
        "user_type": "admin",
        "email": email,
        "created_at": time.time(),
        "last_activity": time.time(),
    }
    return {"session": _serializer.dumps(data)}


def vendor_cookie(email="vendor@test.com"):
    """Create a signed vendor session cookie."""
    data = {
        "user_type": "vendor",
        "email": email,
        "created_at": time.time(),
        "last_activity": time.time(),
    }
    return {"session": _serializer.dumps(data)}


def seed_draft(db, email="vendor@test.com", draft=None):
    """Seed a registration draft in the database."""
    if draft is None:
        return
    existing = db.query(RegistrationDraft).filter(RegistrationDraft.email == email).first()
    if existing:
        existing.draft_json = json.dumps(draft)
        existing.updated_at = datetime.now(timezone.utc)
    else:
        db.add(RegistrationDraft(email=email, draft_json=json.dumps(draft)))
    db.commit()


def extract_csrf(html: str) -> str:
    """Extract CSRF token from rendered HTML."""
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token not found"
    return match.group(1)


def seed_admin(db, email="admin@test.com"):
    """Ensure an admin user exists."""
    existing = db.query(AdminUser).filter(AdminUser.email == email).first()
    if not existing:
        db.add(AdminUser(email=email, is_active=True))
        db.commit()


def seed_booth_types(db):
    """Seed booth types (Premium + Regular) and return them."""
    if db.query(BoothType).count() > 0:
        return db.query(BoothType).order_by(BoothType.sort_order).all()
    booths = [
        BoothType(name="Premium", description="Corner spot", total_quantity=20, price=15000, sort_order=1),
        BoothType(name="Regular", description="Standard spot", total_quantity=80, price=10000, sort_order=2),
    ]
    db.add_all(booths)
    db.commit()
    return db.query(BoothType).order_by(BoothType.sort_order).all()


def seed_event(db):
    """Seed event settings (future dates, not currently open)."""
    if db.query(EventSettings).first():
        return
    db.add(EventSettings(
        id=1,
        event_name="Test Event",
        event_start_date=datetime(2026, 10, 17).date(),
        event_end_date=datetime(2026, 10, 18).date(),
        registration_open_date=datetime(2026, 6, 1),
        registration_close_date=datetime(2026, 9, 15, 23, 59, 59),
        vendor_agreement_text="Agreement text.",
    ))
    db.commit()


def seed_event_open(db):
    """Seed event settings with registration currently open."""
    settings = db.query(EventSettings).first()
    if not settings:
        settings = EventSettings(
            id=1,
            event_name="Test Event",
            event_start_date=datetime(2026, 10, 17).date(),
            event_end_date=datetime(2026, 10, 18).date(),
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


def seed_event_future(db):
    """Seed event settings with registration not yet open."""
    settings = db.query(EventSettings).first()
    if not settings:
        settings = EventSettings(
            id=1,
            event_name="Test Event",
            event_start_date=datetime(2030, 10, 17).date(),
            event_end_date=datetime(2030, 10, 18).date(),
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


def seed_event_closed(db):
    """Seed event settings with registration already closed."""
    settings = db.query(EventSettings).first()
    if not settings:
        settings = EventSettings(
            id=1,
            event_name="Test Event",
            event_start_date=datetime(2020, 10, 17).date(),
            event_end_date=datetime(2020, 10, 18).date(),
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


def make_insurance_doc(db, email="vendor@test.com", approved=False,
                       stored_filename="abc123.pdf", original_filename="insurance.pdf"):
    """Create a test insurance document record."""
    doc = InsuranceDocument(
        email=email,
        original_filename=original_filename,
        stored_filename=stored_filename,
        content_type="application/pdf",
        file_size=1024,
        is_approved=approved,
        approved_by="admin@test.com" if approved else None,
        approved_at=datetime.now(timezone.utc) if approved else None,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def make_registration(db, booth_type_id, status="pending", email="vendor@test.com",
                      reg_id="ANM-2026-0001", business_name="Test Biz",
                      stripe_pi_id=None, amount_paid=None):
    """Create a test registration."""
    reg = Registration(
        registration_id=reg_id,
        email=email,
        business_name=business_name,
        contact_name="Test Vendor",
        phone="555-0100",
        category="food",
        description="Delicious food",
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
# Webhook event builder
# ========================================

def build_webhook_event(event_id, event_type, data_object):
    """Build a Stripe webhook event dict for testing."""
    return {
        "id": event_id,
        "type": event_type,
        "data": {"object": data_object},
    }


# ========================================
# Async action helpers (for user-story tests)
# ========================================

async def register_vendor(client, db, email="vendor@test.com", booth_type_id=None,
                           cookies=None, **form_overrides):
    """Register a vendor through the full UI flow. Returns the Registration."""
    seed_event_open(db)
    booths = seed_booth_types(db)

    if booth_type_id is None:
        booth_type_id = booths[0].id
    if cookies is None:
        cookies = vendor_cookie(email)

    # Step 1: fill out the form
    page = await client.get("/vendor/register", cookies=cookies)
    csrf = extract_csrf(page.text)

    form_data = {
        "csrf_token": csrf,
        "contact_name": "Test Vendor",
        "email": email,
        "phone": "555-0100",
        "business_name": "Test Biz",
        "category": "food",
        "description": "Delicious food",
        "booth_type_id": str(booth_type_id),
        "agreement_accepted": "yes",
    }
    form_data.update(form_overrides)

    resp = await client.post("/vendor/register/step1", data=form_data, cookies=cookies)
    assert resp.status_code == 303, f"Step1 failed: {resp.status_code}"

    # Step 2: submit from review page
    page = await client.get("/vendor/register", cookies=cookies)
    csrf = extract_csrf(page.text)

    with patch("app.routes.vendor.send_submission_confirmation_email"), \
         patch("app.routes.vendor.send_admin_notification_email"):
        resp = await client.post(
            "/vendor/register/submit",
            data={"csrf_token": csrf},
            cookies=cookies,
        )
    assert resp.status_code == 303, f"Submit failed: {resp.status_code}"

    reg = (
        db.query(Registration)
        .filter(Registration.email == email)
        .order_by(Registration.id.desc())
        .first()
    )
    assert reg is not None, "Registration not found after submit"
    return reg


async def approve_registration(client, db, registration_id, admin_cookies=None):
    """Approve a registration through the admin UI. Returns the Registration."""
    seed_admin(db)
    if admin_cookies is None:
        admin_cookies = admin_cookie()

    detail = await client.get(
        f"/admin/registrations/{registration_id}", cookies=admin_cookies,
    )
    csrf = extract_csrf(detail.text)

    with patch("app.routes.admin.send_approval_email"):
        resp = await client.post(
            f"/admin/registrations/{registration_id}/approve",
            data={"csrf_token": csrf},
            cookies=admin_cookies,
        )
    assert resp.status_code == 303, f"Approve failed: {resp.status_code}"

    reg = db.query(Registration).filter(
        Registration.registration_id == registration_id,
    ).first()
    db.refresh(reg)
    return reg


async def pay_registration(client, db, registration_id, vendor_cookies=None):
    """Pay for a registration via vendor UI + webhook. Returns the Registration."""
    reg = db.query(Registration).filter(
        Registration.registration_id == registration_id,
    ).first()
    assert reg is not None, f"Registration {registration_id} not found"

    if vendor_cookies is None:
        vendor_cookies = vendor_cookie(reg.email)

    seed_event(db)  # idempotent; ensures settings exist for payment page

    # Initiate payment
    detail = await client.get(
        f"/vendor/registration/{registration_id}", cookies=vendor_cookies,
    )
    csrf = extract_csrf(detail.text)

    with patch("app.routes.vendor.create_payment_intent", return_value="pi_secret_test"):
        resp = await client.post(
            f"/vendor/registration/{registration_id}/pay",
            data={"csrf_token": csrf},
            cookies=vendor_cookies,
        )
    assert resp.status_code == 200, f"Pay failed: {resp.status_code}"

    # Mock doesn't set stripe_payment_intent_id — do it manually
    pi_id = f"pi_test_{registration_id}"
    reg.stripe_payment_intent_id = pi_id
    db.commit()

    # Calculate amount (approved_price is set during approval)
    amount = reg.approved_price if reg.approved_price is not None else 15000

    # Simulate Stripe webhook
    event = build_webhook_event(
        f"evt_{registration_id}",
        "payment_intent.succeeded",
        {"id": pi_id, "amount": amount},
    )

    with patch("app.routes.webhooks.stripe.Webhook.construct_event", return_value=event), \
         patch("app.routes.webhooks.send_payment_confirmation_email"):
        resp = await client.post(
            "/api/webhooks/stripe",
            content=json.dumps(event),
            headers={"stripe-signature": "test_sig"},
        )
    assert resp.status_code == 200, f"Webhook failed: {resp.status_code}"

    db.refresh(reg)
    return reg


async def cancel_registration(client, db, registration_id, admin_cookies=None,
                               refund_amount=None, reason=None):
    """Cancel a paid registration with refund. Returns the Registration."""
    seed_admin(db)
    if admin_cookies is None:
        admin_cookies = admin_cookie()

    reg = db.query(Registration).filter(
        Registration.registration_id == registration_id,
    ).first()
    assert reg is not None, f"Registration {registration_id} not found"

    if refund_amount is None:
        refund_amount = f"{(reg.amount_paid or 0) / 100:.2f}"
    if reason is None:
        reason = "Cancellation requested"

    detail = await client.get(
        f"/admin/registrations/{registration_id}", cookies=admin_cookies,
    )
    csrf = extract_csrf(detail.text)

    with patch("app.routes.admin.create_refund"), \
         patch("app.routes.admin.send_refund_email"):
        resp = await client.post(
            f"/admin/registrations/{registration_id}/cancel",
            data={
                "csrf_token": csrf,
                "refund_amount": refund_amount,
                "reversal_reason": reason,
            },
            cookies=admin_cookies,
            follow_redirects=False,
        )
    assert resp.status_code == 303, f"Cancel failed: {resp.status_code}"

    db.refresh(reg)
    return reg
