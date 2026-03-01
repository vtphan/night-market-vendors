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
from app.services.email import send_payment_confirmation_email, send_admin_notification_email

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
        logger.info(
            "Registration %s is %s, not approved — skipping",
            registration.registration_id,
            registration.status,
        )
        return

    try:
        transition_status(db, registration, "paid", _commit=False)
    except ValueError as e:
        logger.error("Failed to confirm registration %s: %s", registration.registration_id, e)
        return

    registration.amount_paid = payment_intent["amount"]
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

    if registration.status == "cancelled":
        logger.info("Registration %s already cancelled, skipping refund webhook", registration.registration_id)
        return

    logger.info(
        "Received charge.refunded for registration %s (status: %s) — logged for reconciliation",
        registration.registration_id,
        registration.status,
    )
