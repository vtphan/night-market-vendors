import logging
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, BackgroundTasks, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import STRIPE_WEBHOOK_SECRET, APP_URL
from app.database import get_db, get_event_settings
from app.models import Registration, BoothType, StripeEvent, AdminNote
from app.services.registration import transition_status
from app.services.email import send_payment_confirmation_email, send_admin_notification_email, send_admin_alert_email
from app.services.invoice import generate_invoice, INVOICES_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["webhooks"])


@router.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.SignatureVerificationError):
        logger.warning("Invalid webhook signature")
        return JSONResponse(status_code=400, content={"error": "Invalid signature"})

    # Idempotency: insert event record BEFORE handling to prevent races.
    # flush() sends the INSERT to the DB so the unique constraint fires
    # immediately. If a duplicate arrives concurrently, one will get an
    # IntegrityError and skip processing.
    db.add(StripeEvent(stripe_event_id=event["id"], event_type=event["type"]))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        logger.info("Duplicate webhook event %s, skipping", event["id"])
        return JSONResponse(status_code=200, content={"status": "duplicate"})

    try:
        if event["type"] == "payment_intent.succeeded":
            _handle_payment_succeeded(db, event["data"]["object"], background_tasks)
        elif event["type"] == "charge.refunded":
            _handle_charge_refunded(db, event["data"]["object"], background_tasks)
        elif event["type"] == "charge.dispute.created":
            _handle_dispute_created(db, event["data"]["object"], background_tasks)
        else:
            logger.info("Unhandled webhook event type: %s", event["type"])
        # Single commit: event record + handler side effects
        db.commit()
    except Exception:
        # Rollback removes both the event record and handler changes,
        # allowing Stripe to retry the webhook delivery.
        db.rollback()
        logger.exception("Webhook handler failed for event %s", event["id"])
        return JSONResponse(status_code=500, content={"error": "Handler failed"})

    return JSONResponse(status_code=200, content={"status": "ok"})


def _handle_payment_succeeded(db: Session, payment_intent: dict, background_tasks: BackgroundTasks):
    pi_id = payment_intent["id"]
    # Lock the row to prevent races with admin status changes (e.g. revoking
    # approval concurrently).  with_for_update() is a no-op on SQLite.
    registration = db.query(Registration).filter(
        Registration.stripe_payment_intent_id == pi_id
    ).with_for_update().first()

    if not registration:
        # Fallback: the PI ID on the registration may have been overwritten
        # (e.g. a transient Stripe API error during PI retrieval caused
        # create_payment_intent to create a new PI).  The original PI's
        # metadata still carries the registration_id, so try that.
        reg_id = payment_intent.get("metadata", {}).get("registration_id")
        if reg_id:
            registration = db.query(Registration).filter(
                Registration.registration_id == reg_id
            ).with_for_update().first()
            if registration:
                logger.warning(
                    "PaymentIntent %s matched registration %s via metadata fallback "
                    "(current PI on record: %s)",
                    pi_id, reg_id, registration.stripe_payment_intent_id,
                )

    if not registration:
        logger.warning("No registration found for PaymentIntent %s", pi_id)
        return

    if registration.status != "approved":
        # Payment succeeded for a non-approved registration (e.g. vendor's
        # payment was in-flight when admin revoked approval).  Accept the
        # payment instead of auto-refunding — admin can cancel & refund manually.
        old_status = registration.status
        amount = payment_intent["amount"]
        logger.warning(
            "Registration %s is '%s', not approved — accepting payment and setting to paid (PI %s)",
            registration.registration_id, old_status, pi_id,
        )
        registration.status = "paid"
        registration.amount_paid = amount
        system_note = AdminNote(
            registration_id=registration.registration_id,
            admin_email="[System]",
            text=(
                f"Payment completed while status was '{old_status}'. "
                f"Vendor payment was already in progress when approval was revoked. "
                f"Review and cancel if needed."
            ),
        )
        db.add(system_note)
        registration.concern_status = "yes"

        background_tasks.add_task(
            send_admin_alert_email,
            f"Payment received for non-approved registration — {registration.registration_id}",
            f"Registration {registration.registration_id} was '{old_status}' when payment "
            f"succeeded. The payment has been accepted and status set to 'paid'.\n\n"
            f"PaymentIntent: {pi_id}\n"
            f"Amount: ${amount / 100:.2f}\n\n"
            f"Review the registration and cancel with refund if needed:\n"
            f"{APP_URL}/admin/registrations/{registration.registration_id}",
        )

        booth_type = db.query(BoothType).filter(
            BoothType.id == registration.booth_type_id
        ).first()
        background_tasks.add_task(
            send_payment_confirmation_email,
            registration.email,
            registration.registration_id,
            booth_type.name if booth_type else "Unknown",
            amount,
        )
        return

    try:
        transition_status(db, registration, "paid", _commit=False)
    except ValueError as e:
        logger.error("Failed to confirm registration %s: %s", registration.registration_id, e)
        return

    registration.amount_paid = payment_intent["amount"]

    # Sanity check: verify amount matches expected booth price + fee.
    # Use approved_price (locked at approval time) when available.
    booth_type = db.query(BoothType).filter(
        BoothType.id == registration.booth_type_id
    ).first()
    if booth_type:
        price = registration.approved_price if registration.approved_price is not None else booth_type.price
        expected = price + (registration.processing_fee or 0)
        if payment_intent["amount"] != expected:
            logger.warning(
                "Amount mismatch for %s: Stripe charged %d but expected %d (booth %d + fee %d)",
                registration.registration_id, payment_intent["amount"], expected,
                price, registration.processing_fee or 0,
            )

    # No commit here — the caller (stripe_webhook) commits the entire
    # transaction including the StripeEvent record.

    background_tasks.add_task(
        send_payment_confirmation_email,
        registration.email,
        registration.registration_id,
        booth_type.name if booth_type else "Unknown",
        payment_intent["amount"],
    )

    # Generate invoice PDF
    settings = get_event_settings(db)
    try:
        generate_invoice(
            registration_id=registration.registration_id,
            business_name=registration.business_name,
            contact_name=registration.contact_name,
            email=registration.email,
            phone=registration.phone,
            booth_type_name=booth_type.name if booth_type else "Unknown",
            approved_price_cents=registration.approved_price if registration.approved_price is not None else (booth_type.price if booth_type else 0),
            processing_fee_cents=registration.processing_fee or 0,
            amount_paid_cents=payment_intent["amount"],
            paid_at=datetime.now(timezone.utc),
            org_name=settings.org_name if settings else "",
            org_address=settings.org_address if settings else "",
            org_tax_id=settings.org_tax_id if settings else "",
            event_name=settings.event_name if settings else "",
            stripe_payment_intent_id=payment_intent["id"],
        )
    except Exception:
        logger.exception("Failed to generate invoice for %s", registration.registration_id)

    # Admin notification
    if settings and settings.notify_payment_received:
        background_tasks.add_task(
            send_admin_notification_email,
            "payment_received",
            registration.registration_id,
            registration.business_name,
            f"{APP_URL}/admin/registrations/{registration.registration_id}",
        )

    logger.info("Registration %s confirmed via webhook", registration.registration_id)


def _handle_charge_refunded(db: Session, charge: dict, background_tasks: BackgroundTasks):
    pi_id = charge.get("payment_intent")
    if not pi_id:
        logger.info("charge.refunded event has no payment_intent, skipping")
        return

    registration = db.query(Registration).filter(
        Registration.stripe_payment_intent_id == pi_id
    ).with_for_update().first()

    if not registration:
        # Fallback: match by metadata in case the PI ID was overwritten.
        # charge.refunded events include a payment_intent field but not
        # the PI's metadata directly, so retrieve it from Stripe.
        try:
            pi_obj = stripe.PaymentIntent.retrieve(pi_id)
            reg_id = (pi_obj.metadata or {}).get("registration_id")
            if reg_id:
                registration = db.query(Registration).filter(
                    Registration.registration_id == reg_id
                ).with_for_update().first()
                if registration:
                    logger.warning(
                        "Refund charge PI %s matched registration %s via metadata fallback "
                        "(current PI on record: %s)",
                        pi_id, reg_id, registration.stripe_payment_intent_id,
                    )
        except stripe.StripeError:
            logger.warning("Could not retrieve PI %s for metadata fallback", pi_id)

    if not registration:
        logger.warning("No registration found for refund charge PI %s", pi_id)
        return

    # Sync refund_amount from Stripe's authoritative total.
    # This covers refunds initiated via Stripe Dashboard (outside the app).
    stripe_refunded = charge.get("amount_refunded", 0)
    local_refunded = registration.refund_amount or 0
    if stripe_refunded > local_refunded:
        logger.info(
            "Updating refund_amount for %s from %d to %d (Stripe-authoritative)",
            registration.registration_id, local_refunded, stripe_refunded,
        )
        registration.refund_amount = stripe_refunded

    if registration.status == "cancelled":
        logger.info("Registration %s already cancelled, skipping further refund processing", registration.registration_id)
        return

    # If fully refunded (e.g. via Stripe Dashboard) and still "paid", transition to cancelled.
    amount_paid = registration.amount_paid or 0
    if registration.status == "paid" and amount_paid > 0 and stripe_refunded >= amount_paid:
        try:
            transition_status(
                db, registration, "cancelled",
                reversal_reason="Fully refunded via Stripe Dashboard",
                _commit=False,
            )
            db.add(AdminNote(
                registration_id=registration.registration_id,
                admin_email="[System]",
                text=(
                    f"Full refund (${stripe_refunded / 100:.2f}) "
                    f"detected via Stripe Dashboard. Registration auto-cancelled."
                ),
            ))
            registration.concern_status = "yes"
            logger.info(
                "Registration %s auto-cancelled after full refund via Stripe Dashboard",
                registration.registration_id,
            )
            background_tasks.add_task(
                send_admin_alert_email,
                f"UNEXPECTED: Registration auto-cancelled after Stripe Dashboard refund — {registration.registration_id}",
                f"A full refund was issued directly in the Stripe Dashboard, bypassing "
                f"the app's Cancel & Refund flow. This is not normal operation — all "
                f"cancellations and refunds should go through the app for proper "
                f"tracking and vendor notification.\n\n"
                f"Registration {registration.registration_id} ({registration.business_name}) "
                f"was fully refunded (${stripe_refunded / 100:.2f}) and has been "
                f"auto-cancelled to keep inventory accurate.\n\n"
                f"Please investigate who issued this refund and why.\n"
                f"The vendor ({registration.email}) has NOT been notified.\n\n"
                f"PaymentIntent: {pi_id}\n"
                f"Review: {APP_URL}/admin/registrations/{registration.registration_id}",
            )
        except ValueError:
            logger.exception("Failed to auto-cancel registration %s after full refund", registration.registration_id)
    elif stripe_refunded > local_refunded:
        # Partial refund via Stripe Dashboard — note it for admin visibility
        db.add(AdminNote(
            registration_id=registration.registration_id,
            admin_email="[System]",
            text=(
                f"Partial refund (${stripe_refunded / 100:.2f} total) "
                f"detected via Stripe Dashboard."
            ),
        ))
        registration.concern_status = "yes"
        background_tasks.add_task(
            send_admin_alert_email,
            f"UNEXPECTED: Partial refund issued via Stripe Dashboard — {registration.registration_id}",
            f"A partial refund was issued directly in the Stripe Dashboard, bypassing "
            f"the app's Cancel & Refund flow. This is not normal operation — all "
            f"refunds should go through the app for proper tracking and vendor "
            f"notification.\n\n"
            f"Registration {registration.registration_id} ({registration.business_name}) "
            f"received a partial refund "
            f"(${stripe_refunded / 100:.2f} of ${amount_paid / 100:.2f}). "
            f"The registration remains 'paid' — no automatic status change.\n\n"
            f"Please investigate who issued this refund and why.\n"
            f"The vendor ({registration.email}) has NOT been notified.\n\n"
            f"PaymentIntent: {pi_id}\n"
            f"Review: {APP_URL}/admin/registrations/{registration.registration_id}",
        )
        logger.info(
            "Received charge.refunded for registration %s (status: %s) — logged for reconciliation",
            registration.registration_id,
            registration.status,
        )
    else:
        logger.info(
            "Received charge.refunded for registration %s (status: %s) — no new refund amount",
            registration.registration_id,
            registration.status,
        )


def _handle_dispute_created(db: Session, dispute: dict, background_tasks: BackgroundTasks):
    """Alert admins when a customer files a chargeback/dispute."""
    pi_id = dispute.get("payment_intent")
    amount = dispute.get("amount", 0)
    reason = dispute.get("reason", "unknown")
    dispute_id = dispute.get("id", "unknown")

    registration = None
    if pi_id:
        registration = db.query(Registration).filter(
            Registration.stripe_payment_intent_id == pi_id
        ).first()

    reg_id = registration.registration_id if registration else "unknown"
    biz_name = registration.business_name if registration else "unknown"

    logger.warning(
        "Dispute %s created for PI %s (registration %s, amount %d, reason: %s)",
        dispute_id, pi_id, reg_id, amount, reason,
    )

    background_tasks.add_task(
        send_admin_alert_email,
        f"URGENT: Payment dispute filed — {reg_id}",
        f"A customer has filed a payment dispute (chargeback).\n\n"
        f"Dispute ID: {dispute_id}\n"
        f"Registration: {reg_id}\n"
        f"Business: {biz_name}\n"
        f"Amount: ${amount / 100:.2f}\n"
        f"Reason: {reason}\n\n"
        f"Action required: Respond to this dispute in the Stripe Dashboard "
        f"before the deadline to avoid losing the funds.",
    )
