import logging

import stripe
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import STRIPE_WEBHOOK_SECRET
from app.database import get_db
from app.models import Registration, BoothType, StripeEvent
from app.services.registration import transition_status
from app.services.email import send_payment_confirmation_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["webhooks"])


@router.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.SignatureVerificationError):
        logger.warning("Invalid webhook signature")
        return JSONResponse(status_code=400, content={"error": "Invalid signature"})

    # Idempotency check
    existing = db.query(StripeEvent).filter(
        StripeEvent.stripe_event_id == event["id"]
    ).first()
    if existing:
        logger.info("Duplicate webhook event %s, skipping", event["id"])
        return JSONResponse(status_code=200, content={"status": "duplicate"})

    # Record event
    db.add(StripeEvent(stripe_event_id=event["id"], event_type=event["type"]))
    db.commit()

    if event["type"] == "payment_intent.succeeded":
        _handle_payment_succeeded(db, event["data"]["object"])
    elif event["type"] == "charge.refunded":
        _handle_charge_refunded(db, event["data"]["object"])
    else:
        logger.info("Unhandled webhook event type: %s", event["type"])

    return JSONResponse(status_code=200, content={"status": "ok"})


def _handle_payment_succeeded(db: Session, payment_intent: dict):
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
        transition_status(db, registration, "paid")
    except ValueError as e:
        logger.error("Failed to confirm registration %s: %s", registration.registration_id, e)
        return

    registration.amount_paid = payment_intent["amount"]
    db.commit()

    booth_type = db.query(BoothType).filter(
        BoothType.id == registration.booth_type_id
    ).first()

    send_payment_confirmation_email(
        registration.email,
        registration.registration_id,
        booth_type.name if booth_type else "Unknown",
        payment_intent["amount"],
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
