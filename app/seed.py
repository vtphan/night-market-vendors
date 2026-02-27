import json
import logging
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import ADMIN_EMAILS
from app.models import BoothType, EventSettings, AdminUser

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "event.json"


def seed_event_data(db: Session) -> None:
    """Seed event_settings and booth_types from config/event.json. Idempotent."""
    with open(CONFIG_PATH) as f:
        data = json.load(f)

    # Seed event_settings (single row)
    existing = db.query(EventSettings).first()
    if not existing:
        evt = data["event"]
        settings = EventSettings(
            id=1,
            event_name=evt["name"],
            event_start_date=date.fromisoformat(evt["start_date"]),
            event_end_date=date.fromisoformat(evt["end_date"]),
            registration_open_date=datetime.fromisoformat(evt["registration_open_date"]),
            registration_close_date=datetime.fromisoformat(evt["registration_close_date"]),
            vendor_agreement_text=evt["vendor_agreement_text"],
            front_page_content=evt.get("front_page_content", "Welcome! Check back for updates about vendor registration."),
            banner_text=evt.get("banner_text", ""),
            contact_email=evt.get("contact_email", ""),
            payment_instructions=evt.get("payment_instructions", ""),
            insurance_instructions=evt.get("insurance_instructions", ""),
            processing_fee_percent=evt.get("processing_fee_percent", 2.9),
            processing_fee_flat_cents=evt.get("processing_fee_flat_cents", 30),
            refund_policy=evt.get("refund_policy", ""),
            refund_presets=evt.get("refund_presets", "100,75,50,25,0"),
        )
        db.add(settings)
        db.commit()
        logger.info("Seeded event_settings")
    else:
        logger.info("event_settings already exists, skipping seed")

    # Seed booth_types
    existing_count = db.query(BoothType).count()
    if existing_count == 0:
        for bt in data["booth_types"]:
            booth = BoothType(
                name=bt["name"],
                description=bt["description"],
                total_quantity=bt["total_quantity"],
                price=bt["price"],
                sort_order=bt["sort_order"],
            )
            db.add(booth)
        db.commit()
        logger.info("Seeded %d booth_types", len(data["booth_types"]))
    else:
        logger.info("booth_types already exist (%d rows), skipping seed", existing_count)


def bootstrap_admins(db: Session) -> None:
    """Sync admin_users table with ADMIN_EMAILS env var. Idempotent."""
    if not ADMIN_EMAILS:
        logger.info("No ADMIN_EMAILS configured, skipping admin bootstrap")
        return

    admin_emails_set = set(ADMIN_EMAILS)

    # Create or activate admins in the list
    for email in admin_emails_set:
        existing = db.query(AdminUser).filter(AdminUser.email == email).first()
        if existing:
            if not existing.is_active:
                existing.is_active = True
                logger.info("Reactivated admin: %s", email)
        else:
            db.add(AdminUser(email=email, is_active=True))
            logger.info("Created admin: %s", email)

    # Deactivate admins no longer in the list
    all_admins = db.query(AdminUser).filter(AdminUser.is_active == True).all()
    for admin in all_admins:
        if admin.email not in admin_emails_set:
            admin.is_active = False
            logger.info("Deactivated admin: %s", admin.email)

    db.commit()
