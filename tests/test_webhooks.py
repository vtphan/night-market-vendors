"""Tests for Stripe webhook handling, payment service, and admin cancel/refund."""

import json
from unittest.mock import patch, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from tests.helpers import (
    admin_cookie, vendor_cookie, extract_csrf, seed_admin,
    seed_booth_types, seed_event, make_registration,
)


def _build_webhook_event(event_id, event_type, data_object):
    return {
        "id": event_id,
        "type": event_type,
        "data": {"object": data_object},
    }


# ========================================
# Webhook: payment_intent.succeeded
# ========================================

@pytest.mark.anyio
@patch("app.routes.webhooks.send_payment_confirmation_email")
@patch("app.routes.webhooks.stripe.Webhook.construct_event")
async def test_webhook_payment_succeeded(mock_construct, mock_email, db):
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="approved",
                            stripe_pi_id="pi_test_123")

    event_data = _build_webhook_event(
        "evt_test_001", "payment_intent.succeeded",
        {"id": "pi_test_123", "amount": 15000}
    )
    mock_construct.return_value = event_data

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/webhooks/stripe",
            content=json.dumps(event_data),
            headers={"stripe-signature": "test_sig"},
        )

    assert response.status_code == 200
    db.refresh(reg)
    assert reg.status == "paid"
    assert reg.amount_paid == 15000
    mock_email.assert_called_once()


@pytest.mark.anyio
@patch("app.routes.webhooks.send_payment_confirmation_email")
@patch("app.routes.webhooks.stripe.Webhook.construct_event")
async def test_webhook_idempotent(mock_construct, mock_email, db):
    """Duplicate webhook events should be skipped."""
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="approved",
                            stripe_pi_id="pi_test_456")

    event_data = _build_webhook_event(
        "evt_test_dup", "payment_intent.succeeded",
        {"id": "pi_test_456", "amount": 15000}
    )
    mock_construct.return_value = event_data

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # First call
        resp1 = await client.post(
            "/api/webhooks/stripe",
            content=json.dumps(event_data),
            headers={"stripe-signature": "test_sig"},
        )
        assert resp1.status_code == 200

        # Second call (duplicate)
        resp2 = await client.post(
            "/api/webhooks/stripe",
            content=json.dumps(event_data),
            headers={"stripe-signature": "test_sig"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "duplicate"

    # Email sent only once
    mock_email.assert_called_once()


@pytest.mark.anyio
@patch("app.routes.webhooks.stripe.Webhook.construct_event")
async def test_webhook_invalid_signature(mock_construct, db):
    import stripe
    mock_construct.side_effect = stripe.SignatureVerificationError(
        "Invalid signature", "test_sig"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/webhooks/stripe",
            content=b"{}",
            headers={"stripe-signature": "bad_sig"},
        )

    assert response.status_code == 400


@pytest.mark.anyio
@patch("app.routes.webhooks.send_payment_confirmation_email")
@patch("app.routes.webhooks.stripe.Webhook.construct_event")
async def test_webhook_registration_not_found(mock_construct, mock_email, db):
    """Webhook for unknown PaymentIntent should return 200 and not crash."""
    event_data = _build_webhook_event(
        "evt_test_notfound", "payment_intent.succeeded",
        {"id": "pi_nonexistent", "amount": 10000}
    )
    mock_construct.return_value = event_data

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/webhooks/stripe",
            content=json.dumps(event_data),
            headers={"stripe-signature": "test_sig"},
        )

    assert response.status_code == 200
    mock_email.assert_not_called()


@pytest.mark.anyio
@patch("app.routes.webhooks.stripe.Webhook.construct_event")
async def test_webhook_charge_refunded(mock_construct, db):
    """charge.refunded should return 200."""
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, status="cancelled",
                      stripe_pi_id="pi_refund_test", reg_id="ANM-2026-0010")

    event_data = _build_webhook_event(
        "evt_test_refund", "charge.refunded",
        {"id": "ch_test", "payment_intent": "pi_refund_test", "amount_refunded": 15000}
    )
    mock_construct.return_value = event_data

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/webhooks/stripe",
            content=json.dumps(event_data),
            headers={"stripe-signature": "test_sig"},
        )

    assert response.status_code == 200


# ========================================
# Payment service
# ========================================

@pytest.mark.anyio
@patch("app.services.payment.stripe.PaymentIntent.create")
async def test_create_payment_intent_for_approved_reg(mock_pi_create, db):
    from app.services.payment import create_payment_intent

    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="approved")

    mock_pi_create.return_value = MagicMock(
        id="pi_test_new",
        client_secret="pi_test_new_secret_abc",
    )

    secret = create_payment_intent(db, reg, booths[0])

    assert secret == "pi_test_new_secret_abc"
    db.refresh(reg)
    assert reg.stripe_payment_intent_id == "pi_test_new"
    mock_pi_create.assert_called_once_with(
        amount=15000, currency="usd",
        metadata={"registration_id": "ANM-2026-0001"},
    )


@pytest.mark.anyio
@patch("app.services.payment.stripe.Refund.create")
async def test_create_refund(mock_refund_create, db):
    from app.services.payment import create_refund

    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="paid",
                            stripe_pi_id="pi_confirmed_123", amount_paid=15000)

    mock_refund_create.return_value = MagicMock(id="re_test_123")

    refund = create_refund(db, reg, 15000)

    assert refund.id == "re_test_123"
    db.refresh(reg)
    assert reg.refund_amount == 15000
    mock_refund_create.assert_called_once_with(
        payment_intent="pi_confirmed_123", amount=15000,
    )


# ========================================
# Vendor payment endpoint
# ========================================

@pytest.mark.anyio
@patch("app.routes.vendor.create_payment_intent")
async def test_vendor_pay_creates_intent(mock_create_pi, db):
    seed_event(db)
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="approved")

    mock_create_pi.return_value = "pi_secret_test"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Get CSRF token
        detail_resp = await client.get(
            f"/vendor/registration/{reg.registration_id}",
            cookies=vendor_cookie(),
        )
        csrf = extract_csrf(detail_resp.text)

        response = await client.post(
            f"/vendor/registration/{reg.registration_id}/pay",
            data={"csrf_token": csrf},
            cookies=vendor_cookie(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["client_secret"] == "pi_secret_test"
    assert data["amount"] == 15000
    assert data["booth_type"] == "Premium"


@pytest.mark.anyio
async def test_vendor_pay_rejects_non_approved(db):
    """Payment endpoint should reject non-approved registrations."""
    from app.csrf import generate_csrf_token

    seed_event(db)
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="pending")
    csrf = generate_csrf_token()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/vendor/registration/{reg.registration_id}/pay",
            data={"csrf_token": csrf},
            cookies=vendor_cookie(),
        )

    assert response.status_code == 400
    assert "not approved" in response.json()["error"]


@pytest.mark.anyio
async def test_vendor_pay_rejects_wrong_vendor(db):
    """Payment endpoint should reject access from wrong vendor."""
    from app.csrf import generate_csrf_token

    seed_event(db)
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="approved", email="other@test.com")
    csrf = generate_csrf_token()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/vendor/registration/{reg.registration_id}/pay",
            data={"csrf_token": csrf},
            cookies=vendor_cookie("vendor@test.com"),
        )

    assert response.status_code == 404


# ========================================
# Admin cancel + refund
# ========================================

@pytest.mark.anyio
@patch("app.routes.admin.create_refund")
@patch("app.routes.admin.send_refund_email")
async def test_admin_cancel_paid_registration(mock_email, mock_refund, db):
    seed_admin(db)
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="paid",
                            stripe_pi_id="pi_cancel_test", amount_paid=15000)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detail_resp = await client.get(
            f"/admin/registrations/{reg.registration_id}",
            cookies=admin_cookie(),
        )
        csrf = extract_csrf(detail_resp.text)

        response = await client.post(
            f"/admin/registrations/{reg.registration_id}/cancel",
            data={"csrf_token": csrf, "refund_amount": "150.00", "reversal_reason": "Vendor requested cancellation"},
            cookies=admin_cookie(),
            follow_redirects=False,
        )

    assert response.status_code == 303
    db.refresh(reg)
    assert reg.status == "cancelled"
    mock_refund.assert_called_once()
    mock_email.assert_called_once()


@pytest.mark.anyio
async def test_admin_cancel_rejects_non_paid(db):
    """Cannot cancel a registration that is not paid."""
    seed_admin(db)
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="approved")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detail_resp = await client.get(
            f"/admin/registrations/{reg.registration_id}",
            cookies=admin_cookie(),
        )
        csrf = extract_csrf(detail_resp.text)

        response = await client.post(
            f"/admin/registrations/{reg.registration_id}/cancel",
            data={"csrf_token": csrf, "refund_amount": "100.00"},
            cookies=admin_cookie(),
            follow_redirects=False,
        )

    assert response.status_code == 303
    db.refresh(reg)
    assert reg.status == "approved"  # unchanged


# ========================================
# Vendor registration detail shows payment form
# ========================================

@pytest.mark.anyio
async def test_registration_detail_shows_payment_form_for_approved(db):
    seed_event(db)
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="approved")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/vendor/registration/{reg.registration_id}",
            cookies=vendor_cookie(),
        )

    assert response.status_code == 200
    assert "payment-form" in response.text
    assert "Complete Payment" in response.text


@pytest.mark.anyio
async def test_registration_detail_no_payment_form_for_pending(db):
    seed_event(db)
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="pending")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/vendor/registration/{reg.registration_id}",
            cookies=vendor_cookie(),
        )

    assert response.status_code == 200
    assert "payment-form" not in response.text


# ========================================
# CSV export includes payment fields
# ========================================

@pytest.mark.anyio
async def test_csv_export_includes_payment_fields(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, status="paid",
                      stripe_pi_id="pi_csv_test", amount_paid=15000)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/export", cookies=admin_cookie())

    assert response.status_code == 200
    text = response.text
    assert "Amount Paid" in text
    assert "Refund Amount" in text
    assert "Stripe Payment Intent ID" in text
    assert "$150.00" in text
    assert "pi_csv_test" in text
