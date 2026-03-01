import logging
from datetime import datetime, timedelta, timezone

import stripe
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


def _cancel_stale_payment_intent(payment_intent_id: str) -> None:
    """Best-effort cancel of a Stripe PaymentIntent that is no longer needed."""
    try:
        stripe.PaymentIntent.cancel(payment_intent_id)
        logger.info("Cancelled stale PaymentIntent %s", payment_intent_id)
    except stripe.StripeError:
        logger.warning(
            "Could not cancel PaymentIntent %s (may already be terminal)",
            payment_intent_id,
        )


def transition_status(
    db: Session,
    registration: Registration,
    new_status: str,
    reversal_reason: str | None = None,
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

    old_status = registration.status
    # Track PI to cancel AFTER the DB commit succeeds
    stale_pi_id = None

    registration.status = new_status

    if new_status == "approved":
        registration.approved_at = datetime.now(timezone.utc)
        registration.rejected_at = None
        registration.reversal_reason = None
    elif new_status == "rejected":
        # Keep stripe_payment_intent_id so the webhook auto-refund path can
        # still find this registration if the PI already succeeded.
        if old_status == "approved" and registration.stripe_payment_intent_id:
            stale_pi_id = registration.stripe_payment_intent_id
        registration.rejected_at = datetime.now(timezone.utc)
        registration.approved_at = None
        if reversal_reason:
            registration.reversal_reason = reversal_reason
    elif new_status == "pending":
        # Returning from approved/rejected — store the revoke reason
        if old_status == "approved" and registration.stripe_payment_intent_id:
            stale_pi_id = registration.stripe_payment_intent_id
        registration.rejected_at = None
        registration.approved_at = None
        if reversal_reason:
            registration.reversal_reason = reversal_reason
    elif new_status == "cancelled":
        # Retain approved_at for audit trail; record when cancelled
        registration.cancelled_at = datetime.now(timezone.utc)
        if reversal_reason:
            registration.reversal_reason = reversal_reason

    if _commit:
        db.commit()
        db.refresh(registration)
        # Cancel stale PI only after DB commit succeeds
        if stale_pi_id:
            _cancel_stale_payment_intent(stale_pi_id)
    else:
        # Caller is responsible for committing and cancelling the stale PI.
        # Store it on the registration object for the caller to handle.
        registration._stale_pi_to_cancel = stale_pi_id

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

RATE_LIMIT_MAX = 10
LOW_INVENTORY_THRESHOLD = 3


def check_submission_rate_limit(db: Session, ip_address: str) -> bool:
    """Check if IP is within rate limit by counting recent registrations.

    Returns True if allowed, False if blocked.
    """
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    count = (
        db.query(func.count(Registration.id))
        .filter(
            Registration.agreement_ip_address == ip_address,
            Registration.created_at >= one_hour_ago,
        )
        .scalar()
    )
    return count < RATE_LIMIT_MAX


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
