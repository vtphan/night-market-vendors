import logging

import stripe
from sqlalchemy.orm import Session

from app.config import STRIPE_SECRET_KEY
from app.models import Registration, BoothType

logger = logging.getLogger(__name__)

stripe.api_key = STRIPE_SECRET_KEY


def calculate_processing_fee(price_cents: int, fee_percent: float, fee_flat_cents: int) -> int:
    """Returns processing fee in cents using pass-through formula.

    Accounts for Stripe charging its fee on the total (including the fee
    itself), so the organizer nets exactly the booth price after Stripe's cut.
    Formula: (rate * price + flat) / (1 - rate)
    """
    rate = fee_percent / 100
    if rate >= 1:
        return fee_flat_cents
    return round((price_cents * rate + fee_flat_cents) / (1 - rate))


_REUSABLE_PI_STATES = {"requires_payment_method", "requires_confirmation", "requires_action"}


def create_payment_intent(
    db: Session,
    registration: Registration,
    booth_type: BoothType,
    processing_fee_cents: int = 0,
) -> str:
    """Create or reuse a Stripe PaymentIntent for an approved registration.

    If a PaymentIntent already exists and is still payable, returns its
    client_secret instead of creating a new one.  This prevents orphaned
    intents when the vendor refreshes the payment page.

    Returns the client_secret for Stripe.js.
    """
    total_amount = booth_type.price + processing_fee_cents

    # Try to reuse an existing PaymentIntent
    if registration.stripe_payment_intent_id:
        try:
            existing = stripe.PaymentIntent.retrieve(registration.stripe_payment_intent_id)
            if existing.status in _REUSABLE_PI_STATES:
                logger.info(
                    "Reusing PaymentIntent %s for registration %s (status: %s)",
                    existing.id,
                    registration.registration_id,
                    existing.status,
                )
                return existing.client_secret
            logger.info(
                "Existing PaymentIntent %s is %s — creating new one",
                existing.id,
                existing.status,
            )
        except stripe.InvalidRequestError:
            logger.info(
                "PaymentIntent %s not found — creating new one",
                registration.stripe_payment_intent_id,
            )

    intent = stripe.PaymentIntent.create(
        amount=total_amount,
        currency="usd",
        metadata={"registration_id": registration.registration_id},
    )

    registration.stripe_payment_intent_id = intent.id
    registration.processing_fee = processing_fee_cents
    db.commit()

    logger.info(
        "Created PaymentIntent %s for registration %s ($%.2f, fee $%.2f)",
        intent.id,
        registration.registration_id,
        total_amount / 100,
        processing_fee_cents / 100,
    )
    return intent.client_secret


def create_refund(db: Session, registration: Registration, amount_cents: int):
    """Create a Stripe refund for a paid registration.

    Sets registration.refund_amount but does NOT commit — the caller is
    responsible for committing the transaction so that the refund and any
    status transition are atomic.

    Returns the Stripe Refund object.
    """
    refund = stripe.Refund.create(
        payment_intent=registration.stripe_payment_intent_id,
        amount=amount_cents,
    )

    registration.refund_amount = (registration.refund_amount or 0) + amount_cents

    logger.info(
        "Created refund for registration %s ($%.2f)",
        registration.registration_id,
        amount_cents / 100,
    )
    return refund
