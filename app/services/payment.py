import logging

import stripe
from sqlalchemy.orm import Session

from app.config import STRIPE_SECRET_KEY
from app.models import Registration, BoothType

logger = logging.getLogger(__name__)

stripe.api_key = STRIPE_SECRET_KEY


def create_payment_intent(db: Session, registration: Registration, booth_type: BoothType) -> str:
    """Create a Stripe PaymentIntent for an approved registration.

    Returns the client_secret for Stripe.js.
    """
    intent = stripe.PaymentIntent.create(
        amount=booth_type.price,
        currency="usd",
        metadata={"registration_id": registration.registration_id},
    )

    registration.stripe_payment_intent_id = intent.id
    db.commit()

    logger.info(
        "Created PaymentIntent %s for registration %s ($%.2f)",
        intent.id,
        registration.registration_id,
        booth_type.price / 100,
    )
    return intent.client_secret


def create_refund(db: Session, registration: Registration, amount_cents: int):
    """Create a Stripe refund for a confirmed registration.

    Returns the Stripe Refund object.
    """
    refund = stripe.Refund.create(
        payment_intent=registration.stripe_payment_intent_id,
        amount=amount_cents,
    )

    registration.refund_amount = amount_cents
    db.commit()

    logger.info(
        "Created refund for registration %s ($%.2f)",
        registration.registration_id,
        amount_cents / 100,
    )
    return refund
