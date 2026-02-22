import logging
import time
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Registration, BoothType

logger = logging.getLogger(__name__)

# --- Status state machine ---

VALID_TRANSITIONS = {
    "pending": ["approved", "rejected"],
    "approved": ["confirmed", "rejected", "pending"],
    "rejected": ["pending"],
    "confirmed": ["cancelled"],
}

CATEGORIES = {
    "food": "Food",
    "beverage": "Beverage",
    "merchandise": "Merchandise",
    "entertainment": "Entertainment",
    "non_profit": "Non-Profit",
    "health_beauty": "Health & Beauty",
    "promotion": "Promotion",
    "other": "Other",
}

ELECTRICAL_EQUIPMENT_OPTIONS = [
    "microwave",
    "fryer",
    "warmer",
    "rice_cooker",
    "griddle",
    "blender",
]

EQUIP_LABELS = {
    "microwave": "Microwave",
    "fryer": "Fryer",
    "warmer": "Warmer",
    "rice_cooker": "Rice Cooker",
    "griddle": "Griddle",
    "blender": "Blender",
}


def transition_status(
    db: Session,
    registration: Registration,
    new_status: str,
    rejection_reason: str | None = None,
) -> Registration:
    """Enforce status state machine. Raises ValueError on invalid transition."""
    allowed = VALID_TRANSITIONS.get(registration.status, [])
    if new_status not in allowed:
        raise ValueError(
            f"Cannot transition from '{registration.status}' to '{new_status}'"
        )

    registration.status = new_status

    if new_status == "approved":
        registration.approved_at = datetime.now(timezone.utc)
    elif new_status == "rejected":
        registration.rejected_at = datetime.now(timezone.utc)
        if rejection_reason:
            registration.rejection_reason = rejection_reason
    elif new_status == "pending":
        # Returning from rejected — clear rejection fields
        registration.rejected_at = None
        registration.rejection_reason = None
        registration.approved_at = None

    db.commit()
    db.refresh(registration)
    logger.info(
        "Registration %s transitioned to %s",
        registration.registration_id,
        new_status,
    )
    return registration


# --- Registration ID generation ---

def generate_registration_id(db: Session) -> str:
    """Generate next ANM-YYYY-NNNN registration ID for the current year."""
    year = datetime.now(timezone.utc).year
    prefix = f"ANM-{year}-"

    last = (
        db.query(Registration)
        .filter(Registration.registration_id.like(f"{prefix}%"))
        .order_by(Registration.id.desc())
        .first()
    )

    if last:
        last_num = int(last.registration_id.split("-")[-1])
        next_num = last_num + 1
    else:
        next_num = 1

    return f"{prefix}{next_num:04d}"


# --- Create registration ---

def create_registration(db: Session, data: dict) -> Registration:
    """Create a new Registration with generated ID and pending status."""
    reg_id = generate_registration_id(db)

    registration = Registration(
        registration_id=reg_id,
        email=data["email"],
        business_name=data["business_name"],
        contact_name=data["contact_name"],
        phone=data["phone"],
        category=data["category"],
        description=data["description"],
        cuisine_type=data.get("cuisine_type"),
        electrical_equipment=data.get("electrical_equipment"),
        electrical_other=data.get("electrical_other"),
        booth_type_id=data["booth_type_id"],
        status="pending",
        agreement_accepted_at=data["agreement_accepted_at"],
        agreement_ip_address=data["agreement_ip_address"],
    )
    db.add(registration)
    db.commit()
    db.refresh(registration)
    logger.info("Created registration %s for %s", reg_id, data["email"])
    return registration


# --- Rate limiting ---

_rate_limit_store: dict[str, list[float]] = {}
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 3600  # 1 hour


def check_submission_rate_limit(ip_address: str) -> bool:
    """Check if IP is within rate limit. Returns True if allowed, False if blocked."""
    now = time.time()
    timestamps = _rate_limit_store.get(ip_address, [])

    # Remove expired entries
    timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    _rate_limit_store[ip_address] = timestamps

    if len(timestamps) >= RATE_LIMIT_MAX:
        return False

    timestamps.append(now)
    _rate_limit_store[ip_address] = timestamps
    return True


def reset_rate_limits():
    """Clear rate limit store. Used in tests."""
    _rate_limit_store.clear()


# --- Inventory ---

def get_inventory(db: Session) -> list[dict]:
    """Return inventory for all active booth types with availability counts."""
    booth_types = (
        db.query(BoothType)
        .filter(BoothType.is_active == True)
        .order_by(BoothType.sort_order)
        .all()
    )

    result = []
    for bt in booth_types:
        counts = _get_booth_counts(db, bt.id)
        result.append({
            "id": bt.id,
            "name": bt.name,
            "description": bt.description,
            "price": bt.price,
            "total_quantity": bt.total_quantity,
            "approved": counts["approved"],
            "confirmed": counts["confirmed"],
            "reserved": counts["approved"] + counts["confirmed"],
            "available": bt.total_quantity - counts["approved"] - counts["confirmed"],
        })
    return result


def _get_booth_counts(db: Session, booth_type_id: int) -> dict:
    """Get approved/confirmed counts for a booth type."""
    approved_count = (
        db.query(func.count(Registration.id))
        .filter(
            Registration.booth_type_id == booth_type_id,
            Registration.status == "approved",
        )
        .scalar()
    )
    confirmed_count = (
        db.query(func.count(Registration.id))
        .filter(
            Registration.booth_type_id == booth_type_id,
            Registration.status == "confirmed",
        )
        .scalar()
    )
    return {"approved": approved_count, "confirmed": confirmed_count}
