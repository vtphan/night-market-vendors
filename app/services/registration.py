import logging
from datetime import datetime, timedelta, timezone

import stripe
from sqlalchemy import func, or_, and_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models import Registration, BoothType, EventSettings

logger = logging.getLogger(__name__)

# --- Status state machine ---

VALID_TRANSITIONS = {
    "pending": ["approved", "rejected", "withdrawn"],
    "approved": ["paid", "rejected", "pending", "withdrawn"],
    "rejected": ["pending"],
    "paid": ["cancelled"],
    "withdrawn": [],
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


def try_cancel_active_payment_intent(registration: Registration) -> tuple[bool, str | None]:
    """Try to cancel an active PaymentIntent before revoking approval.

    Returns (True, None) if safe to proceed, or (False, error_message) if
    the PI cannot be cancelled and the revocation should be blocked.
    """
    if not registration.stripe_payment_intent_id:
        return (True, None)

    try:
        pi = stripe.PaymentIntent.retrieve(registration.stripe_payment_intent_id)
    except stripe.StripeError:
        logger.warning(
            "Could not retrieve PaymentIntent %s",
            registration.stripe_payment_intent_id,
        )
        return (False, "Unable to verify payment status. Please try again or check Stripe Dashboard.")

    if pi.status == "canceled":
        return (True, None)

    if pi.status == "succeeded":
        return (False, "Cannot revoke: payment has already completed. Use Cancel & Refund instead.")

    if pi.status == "processing":
        return (False, "Cannot revoke: payment is being processed. Please wait and try again.")

    # Cancellable states: requires_payment_method, requires_confirmation, requires_action
    try:
        stripe.PaymentIntent.cancel(registration.stripe_payment_intent_id)
        logger.info("Cancelled PaymentIntent %s before revoking approval", registration.stripe_payment_intent_id)
        return (True, None)
    except stripe.StripeError:
        logger.warning(
            "Failed to cancel PaymentIntent %s",
            registration.stripe_payment_intent_id,
        )
        return (False, "Failed to cancel payment. Please try again or check Stripe Dashboard.")


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

    registration.status = new_status

    if new_status == "approved":
        registration.approved_at = datetime.now(timezone.utc)
        registration.rejected_at = None
        registration.reversal_reason = None
        # approved_price is set by approve_with_inventory_check() before
        # calling this function — don't overwrite it here.
    elif new_status == "rejected":
        if old_status == "approved":
            registration.approved_price = None
        registration.rejected_at = datetime.now(timezone.utc)
        registration.approved_at = None
        registration.payment_deadline = None
        registration.last_reminder_sent_at = None
        registration.reminder_count = 0
        if reversal_reason:
            registration.reversal_reason = reversal_reason
    elif new_status == "pending":
        # Returning from approved/rejected — store the revoke reason
        if old_status == "approved":
            registration.approved_price = None
        registration.rejected_at = None
        registration.approved_at = None
        registration.payment_deadline = None
        registration.last_reminder_sent_at = None
        registration.reminder_count = 0
        if reversal_reason:
            registration.reversal_reason = reversal_reason
    elif new_status == "withdrawn":
        if old_status == "approved":
            registration.approved_price = None
        registration.withdrawn_at = datetime.now(timezone.utc)
        registration.payment_deadline = None
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
    SQLite, so a post-commit verification step detects and reverts overbooking
    caused by concurrent approvals. On PostgreSQL it provides true row-level
    locking and the post-commit check acts as a safety net.

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

    # Lock in the booth price at approval time so later price changes
    # don't retroactively affect this vendor's payment amount.
    registration.approved_price = booth_type.price

    # Transition without committing — we still need to set the deadline
    transition_status(db, registration, "approved", _commit=False)

    # Set payment deadline from event settings
    settings = db.query(EventSettings).first()
    deadline_days = settings.payment_deadline_days if settings else 7
    registration.payment_deadline = compute_payment_deadline(
        registration.approved_at, deadline_days
    )
    registration.last_reminder_sent_at = None
    registration.reminder_count = 0

    db.commit()
    db.refresh(registration)

    # Post-commit verification: re-read counts to detect concurrent approval.
    # with_for_update() is a no-op on SQLite, so the pre-commit check alone
    # cannot prevent overbooking under concurrency. If another approval
    # committed between our read and our commit, revert to pending.
    db.refresh(booth_type)
    post_counts = _get_booth_counts(db, registration.booth_type_id)
    post_available = booth_type.total_quantity - post_counts["approved"] - post_counts["paid"]
    if post_available < 0:
        transition_status(
            db, registration, "pending",
            reversal_reason="System: reverted — booth fully booked by concurrent approval",
        )
        raise ValueError(
            f"No {booth_type.name} booths available (concurrent approval detected)"
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
            "withdrawn": counts["withdrawn"],
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


def compute_payment_deadline(approved_at: datetime, deadline_days: int) -> datetime:
    """Return end-of-day UTC on the day that is `deadline_days` after approval."""
    target = approved_at + timedelta(days=deadline_days)
    return target.replace(hour=23, minute=59, second=59, microsecond=0)


def get_unpaid_registrations(db: Session, settings: EventSettings) -> list[dict]:
    """Get all approved-but-unpaid registrations with urgency bands.

    Urgency levels:
    - "normal"    — within R1 window
    - "reminder_1" — past R1, before R2
    - "reminder_2" — past R2, before deadline
    - "overdue"   — past deadline

    Returns list sorted by payment_deadline ascending (most urgent first).
    """
    registrations = (
        db.query(Registration)
        .filter(Registration.status == "approved")
        .order_by(Registration.payment_deadline.asc().nullslast())
        .all()
    )

    now = datetime.now(timezone.utc)
    r1_days = settings.reminder_1_days if settings else 2
    r2_days = settings.reminder_2_days if settings else 5

    result = []
    for reg in registrations:
        if not reg.approved_at:
            continue

        approved_at = reg.approved_at
        if approved_at.tzinfo is None:
            approved_at = approved_at.replace(tzinfo=timezone.utc)

        days_since = (now - approved_at).days

        deadline = reg.payment_deadline
        if deadline:
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            days_until = (deadline - now).total_seconds() / 86400
        else:
            days_until = None

        # Determine urgency
        if deadline and now > deadline:
            urgency = "overdue"
        elif days_since >= r2_days:
            urgency = "reminder_2"
        elif days_since >= r1_days:
            urgency = "reminder_1"
        else:
            urgency = "normal"

        deadline_date = deadline.strftime("%b %d, %Y") if deadline else None

        result.append({
            "registration": reg,
            "days_since_approval": days_since,
            "days_until_deadline": round(days_until, 1) if days_until is not None else None,
            "urgency": urgency,
            "deadline_date": deadline_date,
        })

    return result


def _get_booth_counts(db: Session, booth_type_id: int) -> dict:
    """Get counts by status for a booth type."""
    counts = (
        db.query(Registration.status, func.count(Registration.id))
        .filter(Registration.booth_type_id == booth_type_id)
        .group_by(Registration.status)
        .all()
    )
    result = {"pending": 0, "approved": 0, "paid": 0, "rejected": 0, "cancelled": 0, "withdrawn": 0}
    for status, count in counts:
        if status in result:
            result[status] = count
    return result
