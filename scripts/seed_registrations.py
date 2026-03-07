#!/usr/bin/env python3
"""Seed the database with test registrations from 4 vendors.

Usage:
    python scripts/seed_registrations.py small      # 10 registrations
    python scripts/seed_registrations.py medium     # 20 registrations
    python scripts/seed_registrations.py large      # 40 registrations

    Add --append to keep existing registrations.

Registrations use only pending and approved statuses.
Approved counts never exceed booth inventory (10 per type).
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
        "address": "3720 Alumni Ave",
        "city_state_zip": "Memphis, TN 38152",
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
        "address": "5100 Poplar Ave",
        "city_state_zip": "Memphis, TN 38137",
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
# Registration plans — (vendor_index, booth_type_name, status)
#
# Inventory: Premium=10, Regular=10, Compact=10
# Approved counts per type must stay ≤ 10.
# ---------------------------------------------------------------------------

PLANS = {
    # ------------------------------------------------------------------
    # SMALL — 10 registrations, 4 approved
    # Approved: Premium=1, Regular=2, Compact=1
    # ------------------------------------------------------------------
    "small": [
        # Vendor 0 — vphan@memphis.edu (3 regs)
        (0, "Regular Booth", "approved"),
        (0, "Compact Booth", "pending"),
        (0, "Premium Booth", "pending"),
        # Vendor 1 — thuyadiobooks@gmail.com (3 regs)
        (1, "Premium Booth", "approved"),
        (1, "Regular Booth", "pending"),
        (1, "Compact Booth", "pending"),
        # Vendor 2 — aidangphieu@gmail.com (2 regs)
        (2, "Regular Booth", "approved"),
        (2, "Premium Booth", "pending"),
        # Vendor 3 — moodandmelody1975@gmail.com (2 regs)
        (3, "Compact Booth", "approved"),
        (3, "Regular Booth", "pending"),
    ],

    # ------------------------------------------------------------------
    # MEDIUM — 20 registrations, 8 approved
    # Approved: Premium=3, Regular=3, Compact=2
    # ------------------------------------------------------------------
    "medium": [
        # Vendor 0 (5 regs)
        (0, "Premium Booth", "approved"),
        (0, "Regular Booth", "approved"),
        (0, "Compact Booth", "pending"),
        (0, "Premium Booth", "pending"),
        (0, "Regular Booth", "pending"),
        # Vendor 1 (5 regs)
        (1, "Premium Booth", "approved"),
        (1, "Regular Booth", "pending"),
        (1, "Compact Booth", "approved"),
        (1, "Premium Booth", "pending"),
        (1, "Compact Booth", "pending"),
        # Vendor 2 (5 regs)
        (2, "Premium Booth", "approved"),
        (2, "Regular Booth", "approved"),
        (2, "Compact Booth", "pending"),
        (2, "Regular Booth", "pending"),
        (2, "Compact Booth", "pending"),
        # Vendor 3 (5 regs)
        (3, "Compact Booth", "approved"),
        (3, "Regular Booth", "approved"),
        (3, "Premium Booth", "pending"),
        (3, "Regular Booth", "pending"),
        (3, "Premium Booth", "pending"),
    ],

    # ------------------------------------------------------------------
    # LARGE — 40 registrations, 15 approved
    # Approved: Premium=5, Regular=5, Compact=5
    # ------------------------------------------------------------------
    "large": [
        # Vendor 0 (10 regs) — approved: P=2, R=1, C=1
        (0, "Premium Booth", "approved"),
        (0, "Premium Booth", "approved"),
        (0, "Regular Booth", "approved"),
        (0, "Compact Booth", "approved"),
        (0, "Regular Booth", "pending"),
        (0, "Compact Booth", "pending"),
        (0, "Premium Booth", "pending"),
        (0, "Regular Booth", "pending"),
        (0, "Compact Booth", "pending"),
        (0, "Premium Booth", "pending"),
        # Vendor 1 (10 regs) — approved: P=1, R=2, C=1
        (1, "Premium Booth", "approved"),
        (1, "Regular Booth", "approved"),
        (1, "Regular Booth", "approved"),
        (1, "Compact Booth", "approved"),
        (1, "Compact Booth", "pending"),
        (1, "Premium Booth", "pending"),
        (1, "Regular Booth", "pending"),
        (1, "Compact Booth", "pending"),
        (1, "Premium Booth", "pending"),
        (1, "Regular Booth", "pending"),
        # Vendor 2 (10 regs) — approved: P=1, R=1, C=2
        (2, "Premium Booth", "approved"),
        (2, "Regular Booth", "approved"),
        (2, "Compact Booth", "approved"),
        (2, "Compact Booth", "approved"),
        (2, "Premium Booth", "pending"),
        (2, "Regular Booth", "pending"),
        (2, "Compact Booth", "pending"),
        (2, "Premium Booth", "pending"),
        (2, "Regular Booth", "pending"),
        (2, "Regular Booth", "pending"),
        # Vendor 3 (10 regs) — approved: P=1, R=1, C=1
        (3, "Premium Booth", "approved"),
        (3, "Regular Booth", "approved"),
        (3, "Compact Booth", "approved"),
        (3, "Regular Booth", "pending"),
        (3, "Compact Booth", "pending"),
        (3, "Premium Booth", "pending"),
        (3, "Regular Booth", "pending"),
        (3, "Premium Booth", "pending"),
        (3, "Compact Booth", "pending"),
        (3, "Regular Booth", "pending"),
    ],
}


def validate_plan(plan, label):
    """Verify approved counts don't exceed inventory (10 per type)."""
    from collections import Counter
    approved = Counter(
        booth for _, booth, status in plan if status == "approved"
    )
    for booth_name, count in approved.items():
        if count > 10:
            print(f"ERROR: {label} plan has {count} approved {booth_name} (max 10)")
            sys.exit(1)


def seed(size: str, append: bool = False):
    plan = PLANS[size]
    validate_plan(plan, size)

    db = SessionLocal()
    try:
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

        print(f"\nSeeding {size} dataset ({len(plan)} registrations):\n")

        for i, (vendor_idx, booth_name, status) in enumerate(plan):
            vendor = VENDORS[vendor_idx]
            bt = booth_types.get(booth_name)
            if not bt:
                print(f"WARNING: booth type '{booth_name}' not found, skipping.")
                continue

            reg_id = generate_registration_id(db)

            # Stagger created_at so they look realistic
            created_at = now - timedelta(days=len(plan) - i, hours=i * 2)

            reg = Registration(
                registration_id=reg_id,
                email=vendor["email"],
                business_name=vendor["business_name"],
                contact_name=vendor["contact_name"],
                phone=vendor["phone"],
                category=vendor["category"],
                description=vendor["description"],
                electrical_equipment=vendor.get("electrical_equipment"),
                address=vendor.get("address"),
                city_state_zip=vendor.get("city_state_zip"),
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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    if not args or args[0] not in PLANS:
        print("Usage: python scripts/seed_registrations.py <small|medium|large> [--append]")
        sys.exit(1)

    size = args[0]
    append = "--append" in flags
    seed(size, append)
