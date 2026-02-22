"""Shared test helpers for session cookies, seeding, and registration creation."""

import re
import time
from datetime import datetime, timezone

from app.models import AdminUser, BoothType, EventSettings, Registration
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


def vendor_cookie(email="vendor@test.com", draft=None):
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
        event_date=datetime(2026, 10, 17).date(),
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


def seed_event_future(db):
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


def seed_event_closed(db):
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
