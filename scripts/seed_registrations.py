#!/usr/bin/env python3
"""Seed the database with test registrations from 4 vendors.

Usage:
    python scripts/seed_registrations.py          # fresh seed (deletes existing registrations first)
    python scripts/seed_registrations.py --append  # add to existing data

Vendors get multiple registrations across booth types with a mix of
pending and approved statuses.  Approved counts never exceed booth
inventory (derived from booth_types.total_quantity).
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path so we can import app modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app.models import Registration, BoothType, EventSettings
from app.services.registration import generate_registration_id

# ---------------------------------------------------------------------------
# Vendor definitions
# ---------------------------------------------------------------------------

VENDORS = [
    {
        "email": "vphan@memphis.edu",
        "business_name": "Phan's Pho House",
        "contact_name": "Vinh Phan",
        "phone": "901-555-0101",
        "category": "food",
        "description": "Authentic Vietnamese pho and banh mi sandwiches",
        "electrical_equipment": "warmer,rice_cooker",
    },
    {
        "email": "thuyadiobooks@gmail.com",
        "business_name": "Thuy's Bookshelf",
        "contact_name": "Thuy Adio",
        "phone": "901-555-0102",
        "category": "merchandise",
        "description": "Asian-authored books, zines, and cultural prints",
        "electrical_equipment": None,
    },
    {
        "email": "aidangphieu@gmail.com",
        "business_name": "Phieu Bubble Tea",
        "contact_name": "Aidan Phieu",
        "phone": "901-555-0103",
        "category": "beverage",
        "description": "Hand-crafted bubble tea and fruit smoothies",
        "electrical_equipment": "fryer",
    },
    {
        "email": "moodandmelody1975@gmail.com",
        "business_name": "Mood & Melody Crafts",
        "contact_name": "Melody Tran",
        "phone": "901-555-0104",
        "category": "merchandise",
        "description": "Handmade jewelry, candles, and aromatherapy products",
        "electrical_equipment": None,
    },
]

# ---------------------------------------------------------------------------
# Registration plan
#
# Each tuple: (vendor_index, booth_type_name, status)
#
# Inventory: Premium=1, Regular=2, Compact=1
# Approved caps:  Premium ≤1, Regular ≤2, Compact ≤1
# ---------------------------------------------------------------------------

REGISTRATIONS = [
    # Vendor 0 — vphan@memphis.edu
    (0, "Regular Booth", "approved"),
    (0, "Compact Booth", "pending"),

    # Vendor 1 — thuyadiobooks@gmail.com
    (1, "Premium Booth", "pending"),
    (1, "Regular Booth", "approved"),
    (1, "Compact Booth", "pending"),

    # Vendor 2 — aidangphieu@gmail.com
    (2, "Premium Booth", "approved"),
    (2, "Regular Booth", "pending"),

    # Vendor 3 — moodandmelody1975@gmail.com
    (3, "Compact Booth", "approved"),
    (3, "Regular Booth", "pending"),
    (3, "Premium Booth", "pending"),
]

# Sanity check: approved counts must not exceed inventory
# Premium: 1 approved (vendor 2)   ≤ 1 ✓
# Regular: 2 approved (vendor 0,1) ≤ 2 ✓
# Compact: 1 approved (vendor 3)   ≤ 1 ✓


def seed(append: bool = False):
    db = SessionLocal()
    try:
        # Look up booth types
        booth_types = {bt.name: bt for bt in db.query(BoothType).all()}
        if not booth_types:
            print("ERROR: No booth types found. Run the app once first to seed event data.")
            sys.exit(1)

        settings = db.query(EventSettings).first()
        deadline_days = settings.payment_deadline_days if settings else 7

        if not append:
            deleted = db.query(Registration).delete()
            db.commit()
            if deleted:
                print(f"Cleared {deleted} existing registration(s).")

        now = datetime.now(timezone.utc)
        created = []

        for i, (vendor_idx, booth_name, status) in enumerate(REGISTRATIONS):
            vendor = VENDORS[vendor_idx]
            bt = booth_types.get(booth_name)
            if not bt:
                print(f"WARNING: booth type '{booth_name}' not found, skipping.")
                continue

            reg_id = generate_registration_id(db)

            # Stagger created_at so they look realistic
            created_at = now - timedelta(days=len(REGISTRATIONS) - i, hours=i * 2)

            reg = Registration(
                registration_id=reg_id,
                email=vendor["email"],
                business_name=vendor["business_name"],
                contact_name=vendor["contact_name"],
                phone=vendor["phone"],
                category=vendor["category"],
                description=vendor["description"],
                electrical_equipment=vendor.get("electrical_equipment"),
                booth_type_id=bt.id,
                status=status,
                agreement_accepted_at=created_at,
                agreement_ip_address="127.0.0.1",
                created_at=created_at,
            )

            if status == "approved":
                reg.approved_at = created_at + timedelta(hours=6)
                reg.approved_price = bt.price
                reg.payment_deadline = reg.approved_at + timedelta(days=deadline_days)

            db.add(reg)
            db.commit()
            db.refresh(reg)
            created.append(reg)
            print(f"  {reg.registration_id}  {status:<8}  {booth_name:<16}  {vendor['email']}")

        print(f"\nSeeded {len(created)} registration(s).")

        # Print inventory summary
        print("\nInventory check:")
        for name, bt in sorted(booth_types.items(), key=lambda x: x[1].sort_order):
            approved = db.query(Registration).filter(
                Registration.booth_type_id == bt.id,
                Registration.status.in_(["approved", "paid"]),
            ).count()
            print(f"  {name}: {approved}/{bt.total_quantity} allocated")

    finally:
        db.close()


if __name__ == "__main__":
    append = "--append" in sys.argv
    seed(append=append)
