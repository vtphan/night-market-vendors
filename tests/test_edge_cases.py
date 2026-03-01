"""Tier 7 — Concurrency, Payment, & Resilience Edge Cases (Stories 38–55)."""

import json
import logging
import time

import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy.exc import OperationalError

from app.models import (
    BoothType, EventSettings, InsuranceDocument, OTPCode,
    Registration, RegistrationDraft, StripeEvent,
)
from app.session import _serializer
from tests.helpers import (
    register_vendor,
    approve_registration,
    pay_registration,
    vendor_cookie,
    admin_cookie,
    seed_admin,
    seed_booth_types,
    seed_event_open,
    make_registration,
    build_webhook_event,
    extract_csrf,
    make_insurance_doc,
)

pytestmark = pytest.mark.anyio


# ── Concurrency & Race Conditions ──────────────────────────────────────


async def test_story38_payment_while_revoked_to_pending(client, db):
    """Story 38 (E-A1): Payment succeeds after admin revoked approval to pending."""
    reg = await register_vendor(client, db)
    reg = await approve_registration(client, db, reg.registration_id)

    # Simulate admin revoking approval (status back to pending)
    reg.status = "pending"
    reg.stripe_payment_intent_id = "pi_test_38_pend"
    db.commit()

    event = build_webhook_event("evt_38_pend", "payment_intent.succeeded", {
        "id": "pi_test_38_pend", "amount": 15000,
    })
    with patch("app.routes.webhooks.stripe.Webhook.construct_event", return_value=event), \
         patch("app.routes.webhooks.send_payment_confirmation_email"), \
         patch("app.routes.webhooks.send_admin_alert_email"):
        resp = await client.post("/api/webhooks/stripe",
            content=json.dumps(event), headers={"stripe-signature": "t"})

    assert resp.status_code == 200
    db.refresh(reg)
    assert reg.status == "paid"
    assert reg.amount_paid == 15000
    assert "[System" in (reg.admin_notes or "")
    assert "pending" in reg.admin_notes


async def test_story38_payment_while_revoked_to_rejected(client, db):
    """Story 38 (E-A1): Payment succeeds after admin rejected registration."""
    reg = await register_vendor(client, db)
    reg = await approve_registration(client, db, reg.registration_id)

    reg.status = "rejected"
    reg.stripe_payment_intent_id = "pi_test_38_rej"
    db.commit()

    event = build_webhook_event("evt_38_rej", "payment_intent.succeeded", {
        "id": "pi_test_38_rej", "amount": 15000,
    })
    with patch("app.routes.webhooks.stripe.Webhook.construct_event", return_value=event), \
         patch("app.routes.webhooks.send_payment_confirmation_email"), \
         patch("app.routes.webhooks.send_admin_alert_email"):
        resp = await client.post("/api/webhooks/stripe",
            content=json.dumps(event), headers={"stripe-signature": "t"})

    assert resp.status_code == 200
    db.refresh(reg)
    assert reg.status == "paid"
    assert "rejected" in reg.admin_notes


async def test_story39_payment_intent_reuse(client, db):
    """Story 39 (E-A4): Second payment attempt reuses existing PaymentIntent."""
    reg = await register_vendor(client, db)
    reg = await approve_registration(client, db, reg.registration_id)
    vcook = vendor_cookie(reg.email)

    # Determine expected total amount (approved_price + fee)
    settings = db.query(EventSettings).first()
    from app.services.payment import calculate_processing_fee
    fee = calculate_processing_fee(
        reg.approved_price,
        settings.processing_fee_percent if settings else 0,
        settings.processing_fee_flat_cents if settings else 0,
    )
    total = reg.approved_price + fee

    mock_pi = MagicMock()
    mock_pi.id = "pi_reuse_39"
    mock_pi.client_secret = "cs_reuse_39"
    mock_pi.status = "requires_payment_method"
    mock_pi.amount = total

    # First payment attempt — creates new PI
    detail = await client.get(f"/vendor/registration/{reg.registration_id}", cookies=vcook)
    csrf = extract_csrf(detail.text)
    with patch("app.services.payment.stripe.PaymentIntent.create", return_value=mock_pi) as mock_create:
        resp = await client.post(
            f"/vendor/registration/{reg.registration_id}/pay",
            data={"csrf_token": csrf}, cookies=vcook)
    assert resp.status_code == 200
    assert mock_create.call_count == 1

    # Second attempt — reuses existing PI
    detail = await client.get(f"/vendor/registration/{reg.registration_id}", cookies=vcook)
    csrf = extract_csrf(detail.text)
    with patch("app.services.payment.stripe.PaymentIntent.create") as mock_create2, \
         patch("app.services.payment.stripe.PaymentIntent.retrieve", return_value=mock_pi):
        resp2 = await client.post(
            f"/vendor/registration/{reg.registration_id}/pay",
            data={"csrf_token": csrf}, cookies=vcook)
    assert resp2.status_code == 200
    assert mock_create2.call_count == 0
    assert resp2.json()["client_secret"] == "cs_reuse_39"


async def test_story40_registration_id_collision_retry(client, db):
    """Story 40 (E-A5): Registration ID collision triggers retry with new ID."""
    reg1 = await register_vendor(client, db, email="first40@test.com")
    existing_id = reg1.registration_id

    call_count = [0]

    def mock_gen(db_session):
        call_count[0] += 1
        if call_count[0] == 1:
            return existing_id  # will collide
        return "ANM-2026-9940"

    with patch("app.services.registration.generate_registration_id", side_effect=mock_gen):
        reg2 = await register_vendor(client, db, email="second40@test.com")

    assert reg2 is not None
    assert reg2.registration_id != existing_id
    assert call_count[0] >= 2


# ── Stripe & Payment Edge Cases ────────────────────────────────────────


async def test_story41_db_commit_fails_after_refund(client, db, caplog):
    """Story 41 (E-B1): DB commit fails after Stripe refund — CRITICAL log."""
    reg = await register_vendor(client, db)
    reg = await approve_registration(client, db, reg.registration_id)
    reg = await pay_registration(client, db, reg.registration_id)
    assert reg.status == "paid"

    seed_admin(db)
    acook = admin_cookie()
    detail = await client.get(
        f"/admin/registrations/{reg.registration_id}", cookies=acook)
    csrf = extract_csrf(detail.text)

    # Sabotage: mock create_refund to succeed but make the next commit fail
    def sabotage_commit(db_session, registration, amount):
        registration.refund_amount = (registration.refund_amount or 0) + amount
        real_commit = db_session.commit

        def fail_once():
            db_session.commit = real_commit
            raise OperationalError("simulated disk full", {}, Exception())
        db_session.commit = fail_once

    refund_str = f"{(reg.amount_paid or 0) / 100:.2f}"
    with patch("app.routes.admin.create_refund", side_effect=sabotage_commit), \
         patch("app.routes.admin.send_refund_email"), \
         patch("app.routes.admin.send_admin_alert_email"), \
         caplog.at_level(logging.CRITICAL, logger="app.routes.admin"):
        resp = await client.post(
            f"/admin/registrations/{reg.registration_id}/cancel",
            data={
                "csrf_token": csrf,
                "refund_amount": refund_str,
                "reversal_reason": "Test cancel",
            },
            cookies=acook,
            follow_redirects=False,
        )

    # Route returns the detail template with error flash (200), not 303 redirect
    assert resp.status_code == 200

    # Registration should still be "paid" (commit rolled back)
    fresh = db.query(Registration).filter(
        Registration.registration_id == reg.registration_id
    ).first()
    db.refresh(fresh)
    assert fresh.status == "paid"

    # CRITICAL log emitted
    assert any("DB commit failed AFTER Stripe refund" in r.message for r in caplog.records)


async def test_story42_full_refund_via_dashboard(client, db):
    """Story 42 (E-B2): Full refund via Stripe Dashboard auto-cancels."""
    reg = await register_vendor(client, db)
    reg = await approve_registration(client, db, reg.registration_id)
    reg = await pay_registration(client, db, reg.registration_id)
    amount_paid = reg.amount_paid

    event = build_webhook_event("evt_42", "charge.refunded", {
        "id": "ch_42",
        "payment_intent": reg.stripe_payment_intent_id,
        "amount": amount_paid,
        "amount_refunded": amount_paid,
    })
    with patch("app.routes.webhooks.stripe.Webhook.construct_event", return_value=event), \
         patch("app.routes.webhooks.send_admin_alert_email"):
        resp = await client.post("/api/webhooks/stripe",
            content=json.dumps(event), headers={"stripe-signature": "t"})

    assert resp.status_code == 200
    db.refresh(reg)
    assert reg.status == "cancelled"
    assert reg.refund_amount == amount_paid
    assert "[System" in (reg.admin_notes or "")
    assert "auto-cancelled" in reg.admin_notes.lower()


async def test_story43_partial_refund_via_dashboard(client, db):
    """Story 43 (E-B2): Partial refund via Dashboard updates amount, keeps paid."""
    reg = await register_vendor(client, db)
    reg = await approve_registration(client, db, reg.registration_id)
    reg = await pay_registration(client, db, reg.registration_id)
    amount_paid = reg.amount_paid
    partial = amount_paid // 2

    event = build_webhook_event("evt_43", "charge.refunded", {
        "id": "ch_43",
        "payment_intent": reg.stripe_payment_intent_id,
        "amount": amount_paid,
        "amount_refunded": partial,
    })
    with patch("app.routes.webhooks.stripe.Webhook.construct_event", return_value=event), \
         patch("app.routes.webhooks.send_admin_alert_email"):
        resp = await client.post("/api/webhooks/stripe",
            content=json.dumps(event), headers={"stripe-signature": "t"})

    assert resp.status_code == 200
    db.refresh(reg)
    assert reg.status == "paid"
    assert reg.refund_amount == partial
    assert "[System" in (reg.admin_notes or "")


async def test_story44_chargeback_alerts_admin(client, db):
    """Story 44 (E-B3): Chargeback/dispute triggers admin alert, status unchanged."""
    reg = await register_vendor(client, db)
    reg = await approve_registration(client, db, reg.registration_id)
    reg = await pay_registration(client, db, reg.registration_id)

    event = build_webhook_event("evt_44", "charge.dispute.created", {
        "id": "dp_44",
        "payment_intent": reg.stripe_payment_intent_id,
        "amount": reg.amount_paid,
        "reason": "fraudulent",
    })
    with patch("app.routes.webhooks.stripe.Webhook.construct_event", return_value=event), \
         patch("app.routes.webhooks.send_admin_alert_email") as mock_alert:
        resp = await client.post("/api/webhooks/stripe",
            content=json.dumps(event), headers={"stripe-signature": "t"})

    assert resp.status_code == 200
    db.refresh(reg)
    assert reg.status == "paid"  # unchanged

    # Verify admin alert was sent with dispute details
    mock_alert.assert_called_once()
    alert_args = mock_alert.call_args
    subject = alert_args[0][0] if alert_args[0] else alert_args[1].get("subject", "")
    body = alert_args[0][1] if len(alert_args[0]) > 1 else alert_args[1].get("body", "")
    assert "dispute" in subject.lower() or "dispute" in body.lower()
    assert "dp_44" in body
    assert "fraudulent" in body


async def test_story45_price_change_after_approval(client, db):
    """Story 45 (E-B4): Booth price change doesn't affect approved vendor's amount."""
    reg = await register_vendor(client, db)
    reg = await approve_registration(client, db, reg.registration_id)
    original_price = reg.approved_price
    assert original_price is not None

    # Admin changes booth price
    booth = db.query(BoothType).filter(BoothType.id == reg.booth_type_id).first()
    booth.price = original_price + 5000  # raise by $50
    db.commit()

    # Vendor visits payment page — should see original approved_price
    vcook = vendor_cookie(reg.email)
    resp = await client.get(
        f"/vendor/registration/{reg.registration_id}", cookies=vcook)
    assert resp.status_code == 200

    # POST to pay — amount should use approved_price
    csrf = extract_csrf(resp.text)
    settings = db.query(EventSettings).first()
    from app.services.payment import calculate_processing_fee
    expected_fee = calculate_processing_fee(
        original_price,
        settings.processing_fee_percent if settings else 0,
        settings.processing_fee_flat_cents if settings else 0,
    )
    expected_total = original_price + expected_fee

    mock_pi = MagicMock()
    mock_pi.id = "pi_45"
    mock_pi.client_secret = "cs_45"

    with patch("app.services.payment.stripe.PaymentIntent.create", return_value=mock_pi) as mock_create:
        resp2 = await client.post(
            f"/vendor/registration/{reg.registration_id}/pay",
            data={"csrf_token": csrf}, cookies=vcook)

    assert resp2.status_code == 200
    # Verify PI was created with the approved_price amount, not the new price
    mock_create.assert_called_once()
    called_amount = mock_create.call_args[1]["amount"]
    assert called_amount == expected_total


async def test_story46_fee_change_triggers_pi_recreation(client, db):
    """Story 46 (E-B5): Processing fee change cancels old PI, creates new."""
    reg = await register_vendor(client, db)
    reg = await approve_registration(client, db, reg.registration_id)
    vcook = vendor_cookie(reg.email)

    settings = db.query(EventSettings).first()
    from app.services.payment import calculate_processing_fee
    original_fee = calculate_processing_fee(
        reg.approved_price,
        settings.processing_fee_percent, settings.processing_fee_flat_cents,
    )
    original_total = reg.approved_price + original_fee

    mock_pi_old = MagicMock()
    mock_pi_old.id = "pi_old_46"
    mock_pi_old.client_secret = "cs_old_46"
    mock_pi_old.status = "requires_payment_method"
    mock_pi_old.amount = original_total

    # First call — creates PI
    detail = await client.get(f"/vendor/registration/{reg.registration_id}", cookies=vcook)
    csrf = extract_csrf(detail.text)
    with patch("app.services.payment.stripe.PaymentIntent.create", return_value=mock_pi_old):
        await client.post(
            f"/vendor/registration/{reg.registration_id}/pay",
            data={"csrf_token": csrf}, cookies=vcook)

    # Admin changes processing fee
    settings.processing_fee_flat_cents = (settings.processing_fee_flat_cents or 0) + 500
    db.commit()

    new_fee = calculate_processing_fee(
        reg.approved_price,
        settings.processing_fee_percent, settings.processing_fee_flat_cents,
    )
    new_total = reg.approved_price + new_fee
    assert new_total != original_total

    mock_pi_new = MagicMock()
    mock_pi_new.id = "pi_new_46"
    mock_pi_new.client_secret = "cs_new_46"

    # Second call — old PI amount mismatches, cancel + create new
    detail = await client.get(f"/vendor/registration/{reg.registration_id}", cookies=vcook)
    csrf = extract_csrf(detail.text)
    with patch("app.services.payment.stripe.PaymentIntent.retrieve", return_value=mock_pi_old), \
         patch("app.services.payment.stripe.PaymentIntent.cancel") as mock_cancel, \
         patch("app.services.payment.stripe.PaymentIntent.create", return_value=mock_pi_new) as mock_create:
        resp = await client.post(
            f"/vendor/registration/{reg.registration_id}/pay",
            data={"csrf_token": csrf}, cookies=vcook)

    assert resp.status_code == 200
    mock_cancel.assert_called_once_with("pi_old_46")
    mock_create.assert_called_once()
    assert resp.json()["client_secret"] == "cs_new_46"


# ── Authentication & Session Edge Cases ────────────────────────────────


async def test_story47_otp_email_failure_cleans_up(client, db):
    """Story 47 (E-C2): OTP email failure deletes OTP record, shows retry."""
    seed_event_open(db)

    resp = await client.get("/auth/login")
    csrf = extract_csrf(resp.text)

    with patch("app.routes.auth.send_otp_email", return_value=False), \
         patch.dict("app.routes.auth._otp_ip_counts", clear=True):
        resp = await client.post("/auth/login",
            data={"csrf_token": csrf, "email": "otp47@test.com", "role": "vendor"})

    assert resp.status_code == 500
    assert "try again" in resp.text.lower()

    # OTP record should be cleaned up
    otp = db.query(OTPCode).filter(
        OTPCode.email == "otp47@test.com", OTPCode.used == False
    ).first()
    assert otp is None


async def test_story47_subsequent_otp_succeeds_after_failure(client, db):
    """Story 47 (E-C2): Rate limit not consumed by failed email delivery."""
    seed_event_open(db)

    # First attempt: email fails, OTP cleaned up
    resp = await client.get("/auth/login")
    csrf = extract_csrf(resp.text)
    with patch("app.routes.auth.send_otp_email", return_value=False), \
         patch.dict("app.routes.auth._otp_ip_counts", clear=True):
        await client.post("/auth/login",
            data={"csrf_token": csrf, "email": "otp47b@test.com", "role": "vendor"})

    # Second attempt: email succeeds
    resp = await client.get("/auth/login")
    csrf = extract_csrf(resp.text)
    with patch("app.routes.auth.send_otp_email", return_value=True), \
         patch.dict("app.routes.auth._otp_ip_counts", clear=True):
        resp = await client.post("/auth/login",
            data={"csrf_token": csrf, "email": "otp47b@test.com", "role": "vendor"})

    assert resp.status_code == 200
    assert "verification" in resp.text.lower() or "code" in resp.text.lower()


async def test_story48_session_expiry_preserves_draft(client, db):
    """Story 48 (E-C3): Session expiry mid-form; draft survives re-login."""
    seed_event_open(db)
    booths = seed_booth_types(db)
    email = "expiry48@test.com"
    vcook = vendor_cookie(email)

    # Step 1: fill out the form (creates draft at step 2)
    page = await client.get("/vendor/register", cookies=vcook)
    csrf = extract_csrf(page.text)
    await client.post("/vendor/register/step1", data={
        "csrf_token": csrf,
        "contact_name": "Expiry Test",
        "email": email,
        "phone": "555-4848",
        "business_name": "Draft Survivor",
        "category": "food",
        "description": "Persisted draft",
        "booth_type_id": str(booths[0].id),
        "agreement_accepted": "yes",
    }, cookies=vcook)

    draft = db.query(RegistrationDraft).filter(
        RegistrationDraft.email == email
    ).first()
    assert draft is not None

    # Create expired cookie (25 hours old)
    expired = _serializer.dumps({
        "user_type": "vendor",
        "email": email,
        "created_at": time.time() - 90000,
        "last_activity": time.time() - 90000,
    })
    resp = await client.get("/vendor/register", cookies={"session": expired})
    assert resp.status_code == 303  # redirected to login

    # Re-login with fresh cookie — draft should be preserved
    fresh = vendor_cookie(email)
    resp = await client.get("/vendor/register", cookies=fresh)
    assert resp.status_code == 200
    assert "Draft Survivor" in resp.text


# ── Data & Validation Edge Cases ───────────────────────────────────────


async def test_story49_insurance_reupload_resets_approval(client, db, tmp_path):
    """Story 49 (E-D3): Re-upload clears approval; only one document per email."""
    from app.csrf import generate_csrf_token
    from app.main import app as fastapi_app
    fastapi_app.state.uploads_dir = tmp_path / "insurance"
    fastapi_app.state.uploads_dir.mkdir(parents=True, exist_ok=True)

    email = "ins49@test.com"
    reg = await register_vendor(client, db, email=email)
    vcook = vendor_cookie(email)

    # Seed an already-approved insurance doc directly (Story 8 covers the upload itself)
    doc = make_insurance_doc(db, email=email, approved=True)
    assert doc.is_approved is True

    # Re-upload via POST — the backend resets approval regardless of current state.
    # (Approved docs don't show re-upload form in the UI, but the route handles it.)
    csrf = generate_csrf_token()
    with patch("app.routes.vendor.send_admin_notification_email"):
        resp = await client.post("/vendor/insurance/upload",
            data={"csrf_token": csrf},
            files={"file": ("ins2.pdf", b"%PDF-1.4 second", "application/pdf")},
            cookies=vcook)
    assert resp.status_code == 303

    db.refresh(doc)
    assert doc.is_approved is False
    assert doc.approved_by is None
    assert doc.approved_at is None

    # Still only one document record
    count = db.query(InsuranceDocument).filter(
        InsuranceDocument.email == email
    ).count()
    assert count == 1


async def test_story50_inventory_below_reserved_blocked(client, db):
    """Story 50 (E-D4): Can't reduce inventory below reserved count."""
    reg1 = await register_vendor(client, db, email="inv50a@test.com")
    reg2 = await register_vendor(client, db, email="inv50b@test.com",
                                 business_name="Biz B")
    await approve_registration(client, db, reg1.registration_id)
    await approve_registration(client, db, reg2.registration_id)

    booth = db.query(BoothType).filter(BoothType.id == reg1.booth_type_id).first()
    seed_admin(db)
    acook = admin_cookie()

    # Try to set quantity to 1 (below 2 reserved)
    detail = await client.get("/admin/inventory", cookies=acook)
    csrf = extract_csrf(detail.text)
    resp = await client.post(
        f"/admin/inventory/{booth.id}",
        data={
            "csrf_token": csrf,
            "total_quantity": "1",
            "price": f"{booth.price / 100:.2f}",
            "description": booth.description or "",
        },
        cookies=acook,
        follow_redirects=False,
    )

    # Should show error, not redirect
    assert resp.status_code == 200
    assert "reserved" in resp.text.lower() or "cannot" in resp.text.lower()

    # Quantity unchanged
    db.refresh(booth)
    assert booth.total_quantity == 20  # original seed value

    # Setting to 2 (equal to reserved) should succeed
    csrf2 = extract_csrf(resp.text)
    resp2 = await client.post(
        f"/admin/inventory/{booth.id}",
        data={
            "csrf_token": csrf2,
            "total_quantity": "2",
            "price": f"{booth.price / 100:.2f}",
            "description": booth.description or "",
        },
        cookies=acook,
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    db.refresh(booth)
    assert booth.total_quantity == 2


async def test_story51_malicious_upload_exe_rejected(client, db, tmp_path):
    """Story 51 (E-D5): .exe file rejected."""
    from app.main import app as fastapi_app
    fastapi_app.state.uploads_dir = tmp_path / "insurance"
    fastapi_app.state.uploads_dir.mkdir(parents=True, exist_ok=True)

    reg = await register_vendor(client, db)
    vcook = vendor_cookie(reg.email)

    resp = await client.get("/vendor/insurance", cookies=vcook)
    csrf = extract_csrf(resp.text)
    resp = await client.post("/vendor/insurance/upload",
        data={"csrf_token": csrf},
        files={"file": ("malware.exe", b"MZ\x90", "application/octet-stream")},
        cookies=vcook)

    assert resp.status_code == 200
    assert "not allowed" in resp.text.lower()
    assert db.query(InsuranceDocument).filter(
        InsuranceDocument.email == reg.email
    ).count() == 0


async def test_story51_malicious_upload_oversized_rejected(client, db, tmp_path):
    """Story 51 (E-D5): File exceeding 10 MB rejected."""
    from app.main import app as fastapi_app
    fastapi_app.state.uploads_dir = tmp_path / "insurance"
    fastapi_app.state.uploads_dir.mkdir(parents=True, exist_ok=True)

    reg = await register_vendor(client, db)
    vcook = vendor_cookie(reg.email)

    resp = await client.get("/vendor/insurance", cookies=vcook)
    csrf = extract_csrf(resp.text)
    big_data = b"x" * (10 * 1024 * 1024 + 1)
    resp = await client.post("/vendor/insurance/upload",
        data={"csrf_token": csrf},
        files={"file": ("big.pdf", big_data, "application/pdf")},
        cookies=vcook)

    assert resp.status_code == 200
    assert "too large" in resp.text.lower()
    assert db.query(InsuranceDocument).filter(
        InsuranceDocument.email == reg.email
    ).count() == 0


async def test_story51_malicious_upload_path_traversal_rejected(client, db, tmp_path):
    """Story 51 (E-D5): Path traversal filename rejected (no valid extension)."""
    from app.main import app as fastapi_app
    fastapi_app.state.uploads_dir = tmp_path / "insurance"
    fastapi_app.state.uploads_dir.mkdir(parents=True, exist_ok=True)

    reg = await register_vendor(client, db)
    vcook = vendor_cookie(reg.email)

    resp = await client.get("/vendor/insurance", cookies=vcook)
    csrf = extract_csrf(resp.text)
    resp = await client.post("/vendor/insurance/upload",
        data={"csrf_token": csrf},
        files={"file": ("../../etc/passwd", b"root:x:0", "text/plain")},
        cookies=vcook)

    assert resp.status_code == 200
    assert "not allowed" in resp.text.lower()
    assert db.query(InsuranceDocument).filter(
        InsuranceDocument.email == reg.email
    ).count() == 0


async def test_story52_csv_formula_injection_sanitized(client, db):
    """Story 52 (E-D6): CSV export sanitizes formula-injection attempts."""
    seed_event_open(db)
    booths = seed_booth_types(db)
    seed_admin(db)

    reg = make_registration(
        db, booths[0].id,
        business_name='=CMD("calc")',
        email="formula52@test.com",
        reg_id="ANM-2026-0052",
    )

    acook = admin_cookie()
    resp = await client.get("/admin/export", cookies=acook)
    assert resp.status_code == 200

    csv_text = resp.text
    # Business name should be sanitized with leading single quote
    assert "'=CMD" in csv_text
    # Raw = at cell start should NOT appear (except in the sanitized form)
    lines = [l for l in csv_text.split("\n") if "formula52" in l]
    assert len(lines) == 1
    assert lines[0].count("'=CMD") >= 1


# ── Infrastructure & Resilience ────────────────────────────────────────


async def test_story53_webhook_crash_allows_retry(client, db):
    """Story 53 (E-E1): Handler crash rolls back StripeEvent; Stripe can retry."""
    reg = await register_vendor(client, db)
    reg = await approve_registration(client, db, reg.registration_id)
    reg.stripe_payment_intent_id = "pi_crash_53"
    db.commit()

    event = build_webhook_event("evt_crash_53", "payment_intent.succeeded", {
        "id": "pi_crash_53", "amount": 15000,
    })

    # First attempt: handler crashes
    with patch("app.routes.webhooks.stripe.Webhook.construct_event", return_value=event), \
         patch("app.routes.webhooks._handle_payment_succeeded", side_effect=RuntimeError("boom")):
        resp = await client.post("/api/webhooks/stripe",
            content=json.dumps(event), headers={"stripe-signature": "t"})

    assert resp.status_code == 500

    # StripeEvent should be rolled back (no record)
    se = db.query(StripeEvent).filter(StripeEvent.stripe_event_id == "evt_crash_53").first()
    assert se is None

    # Registration unchanged
    db.refresh(reg)
    assert reg.status == "approved"

    # Retry succeeds
    with patch("app.routes.webhooks.stripe.Webhook.construct_event", return_value=event), \
         patch("app.routes.webhooks.send_payment_confirmation_email"):
        resp = await client.post("/api/webhooks/stripe",
            content=json.dumps(event), headers={"stripe-signature": "t"})

    assert resp.status_code == 200
    db.refresh(reg)
    assert reg.status == "paid"

    # StripeEvent now exists
    se = db.query(StripeEvent).filter(StripeEvent.stripe_event_id == "evt_crash_53").first()
    assert se is not None


async def test_story54_email_failure_doesnt_block(client, db, caplog):
    """Story 54 (E-E3): Non-OTP email failures are logged but don't block."""
    email = "email54@test.com"
    vcook = vendor_cookie(email)
    seed_event_open(db)
    booths = seed_booth_types(db)
    seed_admin(db)
    acook = admin_cookie()

    # Step 1: fill out the registration form (no emails sent here)
    page = await client.get("/vendor/register", cookies=vcook)
    csrf = extract_csrf(page.text)
    resp = await client.post("/vendor/register/step1", data={
        "csrf_token": csrf, "contact_name": "Test Vendor", "email": email,
        "phone": "555-0054", "business_name": "Story54 Biz", "category": "food",
        "description": "Testing email resilience", "booth_type_id": str(booths[0].id),
        "agreement_accepted": "yes",
    }, cookies=vcook)
    assert resp.status_code == 303

    # Step 2: submit — real email functions run, but resend.Emails.send raises
    page = await client.get("/vendor/register", cookies=vcook)
    csrf = extract_csrf(page.text)
    with patch("app.services.email.resend.Emails.send", side_effect=Exception("Resend down")), \
         caplog.at_level(logging.ERROR):
        resp = await client.post("/vendor/register/submit",
            data={"csrf_token": csrf}, cookies=vcook)
    assert resp.status_code == 303

    reg = db.query(Registration).filter(Registration.email == email).first()
    assert reg is not None
    assert reg.status == "pending"
    assert "Failed to send email" in caplog.text

    # Step 3: approve — real email functions run, but resend.Emails.send raises
    detail = await client.get(
        f"/admin/registrations/{reg.registration_id}", cookies=acook)
    csrf = extract_csrf(detail.text)
    caplog.clear()
    with patch("app.services.email.resend.Emails.send", side_effect=Exception("Resend down")), \
         caplog.at_level(logging.ERROR):
        resp = await client.post(
            f"/admin/registrations/{reg.registration_id}/approve",
            data={"csrf_token": csrf}, cookies=acook)
    assert resp.status_code == 303

    db.refresh(reg)
    assert reg.status == "approved"
    assert "Failed to send email" in caplog.text


async def test_story55_stripe_api_failure_graceful_error(client, db):
    """Story 55 (E-E4): Stripe API failure returns friendly error, status unchanged."""
    import stripe as stripe_module

    reg = await register_vendor(client, db)
    reg = await approve_registration(client, db, reg.registration_id)
    vcook = vendor_cookie(reg.email)

    detail = await client.get(
        f"/vendor/registration/{reg.registration_id}", cookies=vcook)
    csrf = extract_csrf(detail.text)

    with patch("app.services.payment.stripe.PaymentIntent.create",
               side_effect=stripe_module.APIConnectionError("Stripe unreachable")):
        resp = await client.post(
            f"/vendor/registration/{reg.registration_id}/pay",
            data={"csrf_token": csrf}, cookies=vcook)

    assert resp.status_code == 502
    data = resp.json()
    assert "unavailable" in data["error"].lower() or "try again" in data["error"].lower()

    db.refresh(reg)
    assert reg.status == "approved"


# ── SQLite Lock Recovery ──────────────────────────────────────────────


async def test_lock_recovery_admin_approve(client, db):
    """SQLite lock: OperationalError on admin approve keeps status pending."""
    from sqlalchemy.orm import Session as SessionCls

    reg = await register_vendor(client, db)
    assert reg.status == "pending"

    seed_admin(db)
    acook = admin_cookie()
    detail = await client.get(
        f"/admin/registrations/{reg.registration_id}", cookies=acook)
    csrf = extract_csrf(detail.text)

    with patch.object(
        SessionCls, "commit",
        side_effect=OperationalError("database is locked", {}, Exception()),
    ), patch("app.routes.admin.send_approval_email"):
        resp = await client.post(
            f"/admin/registrations/{reg.registration_id}/approve",
            data={"csrf_token": csrf},
            cookies=acook,
            follow_redirects=False,
        )

    # Middleware catches unhandled OperationalError → 500
    assert resp.status_code == 500
    assert "unexpected error" in resp.text.lower()

    # Registration status unchanged
    db.expire_all()
    fresh = db.query(Registration).filter(
        Registration.registration_id == reg.registration_id
    ).first()
    assert fresh.status == "pending"


async def test_lock_recovery_insurance_upload(client, db, tmp_path):
    """SQLite lock: OperationalError on insurance upload creates no record."""
    from sqlalchemy.orm import Session as SessionCls
    from app.main import app as fastapi_app

    fastapi_app.state.uploads_dir = tmp_path / "insurance"
    fastapi_app.state.uploads_dir.mkdir(parents=True, exist_ok=True)

    reg = await register_vendor(client, db)
    vcook = vendor_cookie(reg.email)

    resp = await client.get("/vendor/insurance", cookies=vcook)
    csrf = extract_csrf(resp.text)

    with patch.object(
        SessionCls, "commit",
        side_effect=OperationalError("database is locked", {}, Exception()),
    ):
        resp = await client.post(
            "/vendor/insurance/upload",
            data={"csrf_token": csrf},
            files={"file": ("cert.pdf", b"%PDF-1.4 test", "application/pdf")},
            cookies=vcook,
        )

    # Middleware catches re-raised OperationalError → 500
    assert resp.status_code == 500
    assert "unexpected error" in resp.text.lower()

    # No InsuranceDocument record
    db.expire_all()
    count = db.query(InsuranceDocument).filter(
        InsuranceDocument.email == reg.email
    ).count()
    assert count == 0


async def test_lock_recovery_otp_verify(client, db):
    """SQLite lock: OperationalError on OTP verify blocks login, OTP reusable."""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy.orm import Session as SessionCls
    from app.services.otp import hash_otp

    seed_event_open(db)
    email = "otplock@test.com"

    # Create OTP directly with a known code
    otp = OTPCode(
        email=email,
        code_hash=hash_otp("123456"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(otp)
    db.commit()

    # Get CSRF from the verify page
    resp = await client.get(f"/auth/verify?email={email}&role=vendor")
    csrf = extract_csrf(resp.text)

    with patch.object(
        SessionCls, "commit",
        side_effect=OperationalError("database is locked", {}, Exception()),
    ):
        resp = await client.post(
            "/auth/verify",
            data={
                "csrf_token": csrf,
                "email": email,
                "code": "123456",
                "role": "vendor",
            },
        )

    # Middleware catches → 500
    assert resp.status_code == 500
    assert "unexpected error" in resp.text.lower()

    # No session cookie created
    assert "session" not in resp.cookies

    # OTP still unused — vendor can retry
    db.expire_all()
    otp_check = db.query(OTPCode).filter(
        OTPCode.email == email,
        OTPCode.used == False,
    ).first()
    assert otp_check is not None


async def test_lock_recovery_webhook_commit(client, db):
    """SQLite lock: OperationalError in webhook → 500, StripeEvent rolled back."""
    from sqlalchemy.orm import Session as SessionCls

    reg = await register_vendor(client, db)
    reg = await approve_registration(client, db, reg.registration_id)
    reg.stripe_payment_intent_id = "pi_lock_wh"
    db.commit()

    event = build_webhook_event("evt_lock_wh", "payment_intent.succeeded", {
        "id": "pi_lock_wh", "amount": 15000,
    })

    with patch("app.routes.webhooks.stripe.Webhook.construct_event", return_value=event), \
         patch.object(
             SessionCls, "commit",
             side_effect=OperationalError("database is locked", {}, Exception()),
         ):
        resp = await client.post(
            "/api/webhooks/stripe",
            content=json.dumps(event),
            headers={"stripe-signature": "t"},
        )

    # Webhook handler catches exception → 500 (Stripe will retry)
    assert resp.status_code == 500

    # StripeEvent rolled back (not in table)
    db.expire_all()
    se = db.query(StripeEvent).filter(
        StripeEvent.stripe_event_id == "evt_lock_wh"
    ).first()
    assert se is None

    # Registration status unchanged
    fresh = db.query(Registration).filter(
        Registration.registration_id == reg.registration_id
    ).first()
    assert fresh.status == "approved"
