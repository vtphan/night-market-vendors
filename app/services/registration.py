import logging
import time
from datetime import datetime, timezone

from sqlalchemy import func, or_, and_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models import Registration, BoothType

logger = logging.getLogger(__name__)

# --- Status state machine ---

VALID_TRANSITIONS = {
    "pending": ["approved", "rejected"],
    "approved": ["paid", "rejected", "pending"],
    "rejected": ["pending"],
    "paid": ["cancelled"],
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
    _commit: bool = True,
) -> Registration:
    """Enforce status state machine. Raises ValueError on invalid transition.

    Pass _commit=False to defer the commit to the caller (useful when
    batching multiple changes into a single transaction).
    """
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

    if _commit:
        db.commit()
        db.refresh(registration)
    logger.info(
        "Registration %s transitioned to %s",
        registration.registration_id,
        new_status,
    )
    return registration


def approve_with_inventory_check(db: Session, registration: Registration) -> Registration:
    """Atomically check booth availability and approve a registration.

    Uses SELECT ... FOR UPDATE on the BoothType row to serialize concurrent
    approvals for the same booth type. NOTE: with_for_update() is a no-op on
    SQLite — concurrency safety on SQLite relies on the single-writer property
    and our single-threaded request model. On PostgreSQL (production) it
    provides true row-level locking.

    Raises ValueError if inventory is insufficient or the transition is invalid.
    """
    # Lock the booth type row (PostgreSQL) — prevents concurrent approvals
    # from reading stale counts until this transaction commits.
    booth_type = (
        db.query(BoothType)
        .filter(BoothType.id == registration.booth_type_id)
        .with_for_update()
        .first()
    )
    if not booth_type:
        raise ValueError("Booth type not found")

    # Count while holding the lock
    counts = _get_booth_counts(db, registration.booth_type_id)
    available = booth_type.total_quantity - counts["approved"] - counts["paid"]

    if available <= 0:
        raise ValueError(
            f"No {booth_type.name} booths available (0 remaining)"
        )

    # Transition commits the transaction, releasing the lock
    return transition_status(db, registration, "approved")


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
    """Create a new Registration with generated ID and pending status.

    Retries up to 3 times on registration ID collision (concurrent submissions).
    """
    for attempt in range(3):
        reg_id = generate_registration_id(db)

        registration = Registration(
            registration_id=reg_id,
            email=data["email"],
            business_name=data["business_name"],
            contact_name=data["contact_name"],
            phone=data["phone"],
            category=data["category"],
            description=data["description"],
            electrical_equipment=data.get("electrical_equipment"),
            electrical_other=data.get("electrical_other"),
            booth_type_id=data["booth_type_id"],
            status="pending",
            agreement_accepted_at=data["agreement_accepted_at"],
            agreement_ip_address=data["agreement_ip_address"],
        )
        db.add(registration)
        try:
            db.commit()
        except (IntegrityError, OperationalError) as exc:
            db.rollback()
            logger.warning(
                "Registration ID collision or DB contention on %s (attempt %d): %s",
                reg_id, attempt + 1, exc,
            )
            continue
        db.refresh(registration)
        logger.info("Created registration %s for %s", reg_id, data["email"])
        return registration

    raise RuntimeError("Failed to generate unique registration ID after 3 attempts")


# --- Rate limiting ---

_rate_limit_store: dict[str, list[float]] = {}
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 3600  # 1 hour
LOW_INVENTORY_THRESHOLD = 3


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
    """Return inventory for all active booth types with full status breakdown."""
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
            "pending": counts["pending"],
            "approved": counts["approved"],
            "paid": counts["paid"],
            "rejected": counts["rejected"],
            "cancelled": counts["cancelled"],
            "reserved": counts["approved"] + counts["paid"],
            "available": bt.total_quantity - counts["approved"] - counts["paid"],
        })
    return result


def get_booth_availability(db: Session, booth_type_id: int) -> int:
    """Return available booth count for a single booth type."""
    booth_type = db.query(BoothType).filter(BoothType.id == booth_type_id).first()
    if not booth_type:
        return 0
    counts = _get_booth_counts(db, booth_type_id)
    return booth_type.total_quantity - counts["approved"] - counts["paid"]


def get_waitlist_position(db: Session, registration: Registration) -> int | None:
    """Return 1-based waitlist position for a pending registration.

    Returns None if the registration is not pending or if booths are still
    available (i.e. the vendor is not waitlisted).
    """
    if registration.status != "pending":
        return None
    available = get_booth_availability(db, registration.booth_type_id)
    if available > 0:
        return None
    # Count pending registrations submitted before this one (tie-break by ID)
    ahead = (
        db.query(func.count(Registration.id))
        .filter(
            Registration.id != registration.id,
            Registration.booth_type_id == registration.booth_type_id,
            Registration.status == "pending",
            or_(
                Registration.created_at < registration.created_at,
                and_(
                    Registration.created_at == registration.created_at,
                    Registration.id < registration.id,
                ),
            ),
        )
        .scalar()
    )
    return ahead + 1


def _get_booth_counts(db: Session, booth_type_id: int) -> dict:
    """Get counts by status for a booth type."""
    counts = (
        db.query(Registration.status, func.count(Registration.id))
        .filter(Registration.booth_type_id == booth_type_id)
        .group_by(Registration.status)
        .all()
    )
    result = {"pending": 0, "approved": 0, "paid": 0, "rejected": 0, "cancelled": 0}
    for status, count in counts:
        if status in result:
            result[status] = count
    return result
