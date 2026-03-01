import logging

import stripe
from fastapi import APIRouter, BackgroundTasks, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import STRIPE_WEBHOOK_SECRET, APP_URL
from app.database import get_db
from app.models import Registration, BoothType, StripeEvent, EventSettings
from app.services.registration import transition_status
from app.services.email import send_payment_confirmation_email, send_admin_notification_email, send_admin_alert_email

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
            _handle_charge_refunded(db, event["data"]["object"])
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
    registration = db.query(Registration).filter(
        Registration.stripe_payment_intent_id == pi_id
    ).first()

    if not registration:
        logger.warning("No registration found for PaymentIntent %s", pi_id)
        return

    if registration.status != "approved":
        logger.warning(
            "Registration %s is %s, not approved — issuing automatic refund for PI %s",
            registration.registration_id,
            registration.status,
            pi_id,
        )
        try:
            stripe.Refund.create(payment_intent=pi_id)
            logger.info("Auto-refunded PaymentIntent %s (registration %s was %s)",
                        pi_id, registration.registration_id, registration.status)
        except stripe.StripeError:
            logger.exception("Failed to auto-refund PaymentIntent %s — requires manual reconciliation", pi_id)
            # Alert admins so they can manually reconcile in Stripe Dashboard
            background_tasks.add_task(
                send_admin_alert_email,
                f"URGENT: Failed to auto-refund PaymentIntent {pi_id}",
                f"Registration {registration.registration_id} was {registration.status} when payment "
                f"succeeded, but the automatic refund failed. Manual reconciliation is required "
                f"in the Stripe Dashboard.\n\nPaymentIntent: {pi_id}\n"
                f"Amount: ${payment_intent['amount'] / 100:.2f}",
            )
        return

    try:
        transition_status(db, registration, "paid", _commit=False)
    except ValueError as e:
        logger.error("Failed to confirm registration %s: %s", registration.registration_id, e)
        return

    registration.amount_paid = payment_intent["amount"]

    # Sanity check: verify amount matches expected booth price + fee
    booth_type = db.query(BoothType).filter(
        BoothType.id == registration.booth_type_id
    ).first()
    if booth_type:
        expected = booth_type.price + (registration.processing_fee or 0)
        if payment_intent["amount"] != expected:
            logger.warning(
                "Amount mismatch for %s: Stripe charged %d but expected %d (booth %d + fee %d)",
                registration.registration_id, payment_intent["amount"], expected,
                booth_type.price, registration.processing_fee or 0,
            )

    # No commit here — the caller (stripe_webhook) commits the entire
    # transaction including the StripeEvent record.

    booth_type = db.query(BoothType).filter(
        BoothType.id == registration.booth_type_id
    ).first()

    background_tasks.add_task(
        send_payment_confirmation_email,
        registration.email,
        registration.registration_id,
        booth_type.name if booth_type else "Unknown",
        payment_intent["amount"],
    )

    # Admin notification
    settings = db.query(EventSettings).first()
    if settings and settings.notify_payment_received:
        background_tasks.add_task(
            send_admin_notification_email,
            "payment_received",
            registration.registration_id,
            registration.business_name,
            f"{APP_URL}/admin/registrations/{registration.registration_id}",
        )

    logger.info("Registration %s confirmed via webhook", registration.registration_id)


def _handle_charge_refunded(db: Session, charge: dict):
    pi_id = charge.get("payment_intent")
    if not pi_id:
        logger.info("charge.refunded event has no payment_intent, skipping")
        return

    registration = db.query(Registration).filter(
        Registration.stripe_payment_intent_id == pi_id
    ).first()

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

    logger.info(
        "Received charge.refunded for registration %s (status: %s) — logged for reconciliation",
        registration.registration_id,
        registration.status,
    )
