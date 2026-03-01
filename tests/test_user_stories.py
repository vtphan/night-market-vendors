"""End-to-end user story tests."""

import json
import time

import pytest
from unittest.mock import patch, MagicMock

from app.models import Registration, BoothType, InsuranceDocument, StripeEvent, EventSettings
from app.services.registration import get_booth_availability
from tests.helpers import (
    register_vendor,
    approve_registration,
    pay_registration,
    cancel_registration,
    vendor_cookie,
    admin_cookie,
    seed_admin,
    seed_booth_types,
    seed_event_open,
    seed_event_closed,
    make_registration,
    build_webhook_event,
    extract_csrf,
)


# ── Tier 1 — Core Paths ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_story1_happy_path(client, db):
    """Story 1: Register → Approve → Pay."""
    reg = await register_vendor(client, db)
    assert reg.status == "pending"

    reg = await approve_registration(client, db, reg.registration_id)
    assert reg.status == "approved"
    assert reg.approved_price is not None

    reg = await pay_registration(client, db, reg.registration_id)
    assert reg.status == "paid"
    assert reg.amount_paid is not None


@pytest.mark.anyio
async def test_story2_vendor_checks_dashboard(client, db):
    """Story 2: After registering, vendor sees their pending registration on the dashboard."""
    email = "dash@test.com"
    reg = await register_vendor(client, db, email=email)

    resp = await client.get("/vendor/dashboard", cookies=vendor_cookie(email))
    assert resp.status_code == 200
    assert reg.registration_id in resp.text
    assert "pending" in resp.text.lower() or "Pending" in resp.text


@pytest.mark.anyio
async def test_story3_admin_reviews_and_approves(client, db):
    """Story 3: Admin finds the registration on the list page and approves it."""
    reg = await register_vendor(client, db)
    seed_admin(db)
    acook = admin_cookie()

    # Admin list page shows the registration
    resp = await client.get("/admin/registrations", cookies=acook)
    assert resp.status_code == 200
    assert reg.registration_id in resp.text

    # Approve through the helper
    reg = await approve_registration(client, db, reg.registration_id, acook)
    assert reg.status == "approved"


@pytest.mark.anyio
async def test_story4_vendor_sees_payment_page(client, db):
    """Story 4: After approval, vendor sees Stripe payment form on the detail page."""
    email = "pay@test.com"
    reg = await register_vendor(client, db, email=email)
    reg = await approve_registration(client, db, reg.registration_id)

    resp = await client.get(
        f"/vendor/registration/{reg.registration_id}",
        cookies=vendor_cookie(email),
    )
    assert resp.status_code == 200
    # Payment form elements should be present for approved registrations
    assert "stripe" in resp.text.lower() or "payment" in resp.text.lower()


# ── Tier 2 — Common Scenarios ─────────────────────────────────────────


@pytest.mark.anyio
async def test_story5_admin_rejects(client, db):
    """Story 5: Admin rejects a pending registration."""
    reg = await register_vendor(client, db, email="reject@test.com")
    seed_admin(db)
    acook = admin_cookie()

    # Reject inline (Pattern A)
    resp = await client.get(f"/admin/registrations/{reg.registration_id}", cookies=acook)
    csrf = extract_csrf(resp.text)
    with patch("app.routes.admin.send_rejection_email"):
        resp = await client.post(
            f"/admin/registrations/{reg.registration_id}/reject",
            data={"csrf_token": csrf, "reversal_reason": "Not a fit"},
            cookies=acook,
        )
    assert resp.status_code == 303

    db.refresh(reg)
    assert reg.status == "rejected"

    # Vendor dashboard shows rejection
    resp = await client.get("/vendor/dashboard", cookies=vendor_cookie("reject@test.com"))
    assert resp.status_code == 200
    assert reg.registration_id in resp.text


@pytest.mark.anyio
async def test_story6_same_vendor_two_booths(client, db):
    """Story 6: Same vendor registers for two different booth types."""
    email = "multi@test.com"
    booths = seed_booth_types(db)

    reg1 = await register_vendor(
        client, db, email=email, booth_type_id=booths[0].id,
        business_name="Biz A",
    )
    reg2 = await register_vendor(
        client, db, email=email, booth_type_id=booths[1].id,
        business_name="Biz B",
    )

    assert reg1.registration_id != reg2.registration_id
    assert reg1.booth_type_id != reg2.booth_type_id
    assert reg1.status == "pending"
    assert reg2.status == "pending"

    # Dashboard shows both
    resp = await client.get("/vendor/dashboard", cookies=vendor_cookie(email))
    assert resp.status_code == 200
    assert reg1.registration_id in resp.text
    assert reg2.registration_id in resp.text


@pytest.mark.anyio
async def test_story7_inventory_decreases_on_approval(client, db):
    """Story 7: Approving 3 registrations decreases available inventory by 3."""
    booths = seed_booth_types(db)
    booth_id = booths[0].id
    initial_available = get_booth_availability(db, booth_id)

    regs = []
    for i in range(3):
        r = await register_vendor(
            client, db, email=f"inv{i}@test.com",
            booth_type_id=booth_id, business_name=f"Inv Biz {i}",
        )
        regs.append(r)

    for r in regs:
        await approve_registration(client, db, r.registration_id)

    after_available = get_booth_availability(db, booth_id)
    assert after_available == initial_available - 3

    # Admin inventory page loads successfully
    seed_admin(db)
    resp = await client.get("/admin/inventory", cookies=admin_cookie())
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_story8_vendor_uploads_insurance(client, db, tmp_path):
    """Story 8: Vendor uploads insurance document."""
    from app.main import app as fastapi_app
    fastapi_app.state.uploads_dir = tmp_path / "insurance"
    fastapi_app.state.uploads_dir.mkdir(parents=True, exist_ok=True)

    email = "ins@test.com"
    reg = await register_vendor(client, db, email=email)
    vcook = vendor_cookie(email)

    # Upload insurance inline (Pattern C)
    resp = await client.get("/vendor/insurance", cookies=vcook)
    csrf = extract_csrf(resp.text)
    with patch("app.routes.vendor.send_admin_notification_email"):
        resp = await client.post(
            "/vendor/insurance/upload",
            data={"csrf_token": csrf},
            files={"file": ("insurance.pdf", b"%PDF-1.4 test", "application/pdf")},
            cookies=vcook,
        )
    assert resp.status_code == 303

    # Verify insurance document exists
    doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == email).first()
    assert doc is not None
    assert doc.original_filename == "insurance.pdf"
    assert doc.is_approved is False

    # Insurance page shows the upload
    resp = await client.get("/vendor/insurance", cookies=vcook)
    assert resp.status_code == 200
    assert "insurance.pdf" in resp.text


# ── Tier 3 — Admin Corrections ────────────────────────────────────────


@pytest.mark.anyio
async def test_story9_revoke_approval_before_payment(client, db):
    """Story 9: Admin revokes approval (Approved → Rejected) before vendor pays."""
    email = "revoke@test.com"
    reg = await register_vendor(client, db, email=email)
    reg = await approve_registration(client, db, reg.registration_id)
    assert reg.status == "approved"

    # Reject inline (Pattern A)
    seed_admin(db)
    acook = admin_cookie()
    resp = await client.get(f"/admin/registrations/{reg.registration_id}", cookies=acook)
    csrf = extract_csrf(resp.text)
    with patch("app.routes.admin.send_rejection_email"):
        resp = await client.post(
            f"/admin/registrations/{reg.registration_id}/reject",
            data={"csrf_token": csrf, "reversal_reason": "Inventory changed"},
            cookies=acook,
        )
    assert resp.status_code == 303

    db.refresh(reg)
    assert reg.status == "rejected"
    assert reg.reversal_reason == "Inventory changed"

    # Payment page no longer shows payment form
    resp = await client.get(
        f"/vendor/registration/{reg.registration_id}",
        cookies=vendor_cookie(email),
    )
    assert resp.status_code == 200
    assert "stripe_publishable_key" not in resp.text.lower() or "payment-form" not in resp.text


@pytest.mark.anyio
async def test_story10_revoke_rejection_for_rereview(client, db):
    """Story 10: Admin revokes rejection (Rejected → Pending) for re-review."""
    reg = await register_vendor(client, db)
    seed_admin(db)
    acook = admin_cookie()

    # Reject first
    resp = await client.get(f"/admin/registrations/{reg.registration_id}", cookies=acook)
    csrf = extract_csrf(resp.text)
    with patch("app.routes.admin.send_rejection_email"):
        resp = await client.post(
            f"/admin/registrations/{reg.registration_id}/reject",
            data={"csrf_token": csrf, "reversal_reason": "Initial rejection"},
            cookies=acook,
        )
    assert resp.status_code == 303
    db.refresh(reg)
    assert reg.status == "rejected"

    # Unreject (Pattern A)
    resp = await client.get(f"/admin/registrations/{reg.registration_id}", cookies=acook)
    csrf = extract_csrf(resp.text)
    resp = await client.post(
        f"/admin/registrations/{reg.registration_id}/unreject",
        data={"csrf_token": csrf, "reversal_reason": "Reconsidering"},
        cookies=acook,
    )
    assert resp.status_code == 303

    db.refresh(reg)
    assert reg.status == "pending"

    # Admin can now approve again
    reg = await approve_registration(client, db, reg.registration_id, acook)
    assert reg.status == "approved"


@pytest.mark.anyio
async def test_story11_revoke_approval_for_rereview(client, db):
    """Story 11: Admin revokes approval (Approved → Pending) for re-review."""
    email = "unapp@test.com"
    reg = await register_vendor(client, db, email=email)
    reg = await approve_registration(client, db, reg.registration_id)
    assert reg.status == "approved"

    seed_admin(db)
    acook = admin_cookie()

    # Unreject inline (approved → pending)
    resp = await client.get(f"/admin/registrations/{reg.registration_id}", cookies=acook)
    csrf = extract_csrf(resp.text)
    with patch("app.routes.admin.send_approval_revoked_email"):
        resp = await client.post(
            f"/admin/registrations/{reg.registration_id}/unreject",
            data={"csrf_token": csrf, "reversal_reason": "Premature approval"},
            cookies=acook,
        )
    assert resp.status_code == 303

    db.refresh(reg)
    assert reg.status == "pending"

    # Payment page is inaccessible (no payment form)
    resp = await client.get(
        f"/vendor/registration/{reg.registration_id}",
        cookies=vendor_cookie(email),
    )
    assert resp.status_code == 200
    assert "payment-form" not in resp.text

    # Admin can re-approve or reject
    reg = await approve_registration(client, db, reg.registration_id, acook)
    assert reg.status == "approved"


@pytest.mark.anyio
async def test_story12_full_cancellation_with_refund(client, db):
    """Story 12: Paid → Cancelled with refund."""
    email = "cancel@test.com"
    reg = await register_vendor(client, db, email=email)
    reg = await approve_registration(client, db, reg.registration_id)
    reg = await pay_registration(client, db, reg.registration_id)
    assert reg.status == "paid"
    assert reg.amount_paid is not None

    reg = await cancel_registration(client, db, reg.registration_id)
    assert reg.status == "cancelled"
    assert reg.refund_amount is not None

    # Vendor dashboard shows cancellation
    resp = await client.get("/vendor/dashboard", cookies=vendor_cookie(email))
    assert resp.status_code == 200
    assert reg.registration_id in resp.text


@pytest.mark.anyio
async def test_story13_admin_adjusts_inventory(client, db):
    """Story 13: Admin increases booth quantity via inventory endpoint."""
    seed_event_open(db)
    booths = seed_booth_types(db)
    booth = booths[0]
    original_qty = booth.total_quantity
    seed_admin(db)
    acook = admin_cookie()

    resp = await client.get("/admin/inventory", cookies=acook)
    csrf = extract_csrf(resp.text)
    resp = await client.post(
        f"/admin/inventory/{booth.id}",
        data={
            "csrf_token": csrf,
            "total_quantity": str(original_qty + 5),
            "price": f"{booth.price / 100:.2f}",
            "description": booth.description,
        },
        cookies=acook,
    )
    assert resp.status_code == 303

    db.refresh(booth)
    assert booth.total_quantity == original_qty + 5
    assert get_booth_availability(db, booth.id) == original_qty + 5


@pytest.mark.anyio
async def test_story14_admin_changes_registration_dates(client, db):
    """Story 14: Admin extends close date; previously closed registration reopens."""
    seed_event_closed(db)
    vcook = vendor_cookie("dates@test.com")

    # Registration is closed
    resp = await client.get("/vendor/register", cookies=vcook)
    assert resp.status_code == 200
    assert "closed" in resp.text.lower()

    # Admin updates dates to reopen registration
    seed_admin(db)
    acook = admin_cookie()
    settings = db.query(EventSettings).first()

    resp = await client.get("/admin/settings", cookies=acook)
    csrf = extract_csrf(resp.text)
    resp = await client.post(
        "/admin/settings",
        data={
            "csrf_token": csrf,
            "event_name": settings.event_name,
            "event_start_date": "2026-10-17",
            "event_end_date": "2026-10-18",
            "registration_open_date": "2020-01-01T00:00:00",
            "registration_close_date": "2030-12-31T23:59:59",
            "vendor_agreement_text": settings.vendor_agreement_text or "Agreement",
        },
        cookies=acook,
    )
    assert resp.status_code == 303

    # Now registration is open
    resp = await client.get("/vendor/register", cookies=vcook)
    assert resp.status_code == 200
    assert "closed" not in resp.text.lower()


# ── Tier 4 — Guard Rails and Edge Cases ───────────────────────────────


@pytest.mark.anyio
async def test_story15_vendor_cant_pay_before_approval(client, db):
    """Story 15: Vendor can't pay while still pending."""
    from app.csrf import generate_csrf_token

    email = "nopay@test.com"
    reg = await register_vendor(client, db, email=email)
    assert reg.status == "pending"
    vcook = vendor_cookie(email)

    # POST to pay endpoint should fail (not approved)
    csrf = generate_csrf_token()
    resp = await client.post(
        f"/vendor/registration/{reg.registration_id}/pay",
        data={"csrf_token": csrf},
        cookies=vcook,
    )
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_story16_vendor_cant_pay_after_revocation(client, db):
    """Story 16: Vendor can't pay after admin revoked approval."""
    from app.csrf import generate_csrf_token

    email = "revpay@test.com"
    reg = await register_vendor(client, db, email=email)
    reg = await approve_registration(client, db, reg.registration_id)

    # Admin rejects (revoke approval)
    seed_admin(db)
    acook = admin_cookie()
    resp = await client.get(f"/admin/registrations/{reg.registration_id}", cookies=acook)
    csrf = extract_csrf(resp.text)
    with patch("app.routes.admin.send_rejection_email"):
        resp = await client.post(
            f"/admin/registrations/{reg.registration_id}/reject",
            data={"csrf_token": csrf, "reversal_reason": "Changed mind"},
            cookies=acook,
        )
    assert resp.status_code == 303
    db.refresh(reg)
    assert reg.status == "rejected"

    # Vendor tries to pay — should fail
    vcook = vendor_cookie(email)
    csrf = generate_csrf_token()
    resp = await client.post(
        f"/vendor/registration/{reg.registration_id}/pay",
        data={"csrf_token": csrf},
        cookies=vcook,
    )
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_story17_inventory_full_blocks_approval(client, db):
    """Story 17: Admin blocked from approving when inventory is exhausted."""
    seed_event_open(db)
    # Create a booth type with only 2 slots
    bt = BoothType(name="Tiny", description="Small", total_quantity=2, price=5000, sort_order=10)
    db.add(bt)
    db.commit()
    db.refresh(bt)

    regs = []
    for i in range(3):
        r = await register_vendor(
            client, db, email=f"full{i}@test.com",
            booth_type_id=bt.id, business_name=f"Full Biz {i}",
        )
        regs.append(r)

    # First two approvals succeed
    await approve_registration(client, db, regs[0].registration_id)
    await approve_registration(client, db, regs[1].registration_id)
    assert get_booth_availability(db, bt.id) == 0

    # Third approval fails
    seed_admin(db)
    acook = admin_cookie()
    resp = await client.get(f"/admin/registrations/{regs[2].registration_id}", cookies=acook)
    csrf = extract_csrf(resp.text)
    with patch("app.routes.admin.send_approval_email"):
        resp = await client.post(
            f"/admin/registrations/{regs[2].registration_id}/approve",
            data={"csrf_token": csrf},
            cookies=acook,
        )
    # Should render detail page with error (200), not redirect (303)
    assert resp.status_code == 200
    assert "Cannot approve" in resp.text or "0 remaining" in resp.text

    db.refresh(regs[2])
    assert regs[2].status == "pending"


@pytest.mark.anyio
async def test_story18_dashboard_reflects_every_stage(client, db):
    """Story 18: Vendor dashboard shows correct status at each stage."""
    email = "stages@test.com"
    vcook = vendor_cookie(email)
    reg = await register_vendor(client, db, email=email)

    # Pending — dashboard shows "Pending" status label
    resp = await client.get("/vendor/dashboard", cookies=vcook)
    assert reg.registration_id in resp.text
    assert 'status-badge pending' in resp.text
    assert ">Pending<" in resp.text

    # Approved — dashboard shows "Approved" status label
    reg = await approve_registration(client, db, reg.registration_id)
    resp = await client.get("/vendor/dashboard", cookies=vcook)
    assert reg.registration_id in resp.text
    assert 'status-badge approved' in resp.text
    assert ">Approved<" in resp.text

    # Paid — dashboard shows "Paid" status label
    reg = await pay_registration(client, db, reg.registration_id)
    resp = await client.get("/vendor/dashboard", cookies=vcook)
    assert reg.registration_id in resp.text
    assert 'status-badge paid' in resp.text
    assert ">Paid<" in resp.text


@pytest.mark.anyio
async def test_story19_insurance_approval_doesnt_affect_registration(client, db, tmp_path):
    """Story 19: Approving insurance doesn't change registration status."""
    from app.main import app as fastapi_app
    fastapi_app.state.uploads_dir = tmp_path / "insurance"
    fastapi_app.state.uploads_dir.mkdir(parents=True, exist_ok=True)

    email = "insreg@test.com"
    reg = await register_vendor(client, db, email=email)
    reg = await approve_registration(client, db, reg.registration_id)
    assert reg.status == "approved"

    vcook = vendor_cookie(email)
    seed_admin(db)
    acook = admin_cookie()

    # Upload insurance (Pattern C)
    resp = await client.get("/vendor/insurance", cookies=vcook)
    csrf = extract_csrf(resp.text)
    with patch("app.routes.vendor.send_admin_notification_email"):
        resp = await client.post(
            "/vendor/insurance/upload",
            data={"csrf_token": csrf},
            files={"file": ("ins.pdf", b"%PDF-1.4 test", "application/pdf")},
            cookies=vcook,
        )
    assert resp.status_code == 303

    # Approve insurance (Pattern A)
    resp = await client.get(f"/admin/registrations/{reg.registration_id}", cookies=acook)
    csrf = extract_csrf(resp.text)
    resp = await client.post(
        f"/admin/registrations/{reg.registration_id}/insurance/approve",
        data={"csrf_token": csrf},
        cookies=acook,
    )
    assert resp.status_code == 303

    # Registration status unchanged
    db.refresh(reg)
    assert reg.status == "approved"

    # Insurance is approved
    doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == email).first()
    assert doc.is_approved is True


@pytest.mark.anyio
async def test_story20_insurance_revocation(client, db, tmp_path):
    """Story 20: Admin revokes a previously approved insurance document."""
    from app.main import app as fastapi_app
    fastapi_app.state.uploads_dir = tmp_path / "insurance"
    fastapi_app.state.uploads_dir.mkdir(parents=True, exist_ok=True)

    email = "insrev@test.com"
    reg = await register_vendor(client, db, email=email)
    vcook = vendor_cookie(email)
    seed_admin(db)
    acook = admin_cookie()

    # Upload insurance
    resp = await client.get("/vendor/insurance", cookies=vcook)
    csrf = extract_csrf(resp.text)
    with patch("app.routes.vendor.send_admin_notification_email"):
        resp = await client.post(
            "/vendor/insurance/upload",
            data={"csrf_token": csrf},
            files={"file": ("ins.pdf", b"%PDF-1.4 test", "application/pdf")},
            cookies=vcook,
        )
    assert resp.status_code == 303

    # Approve insurance
    resp = await client.get(f"/admin/registrations/{reg.registration_id}", cookies=acook)
    csrf = extract_csrf(resp.text)
    resp = await client.post(
        f"/admin/registrations/{reg.registration_id}/insurance/approve",
        data={"csrf_token": csrf},
        cookies=acook,
    )
    assert resp.status_code == 303
    doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == email).first()
    assert doc.is_approved is True

    # Revoke insurance
    resp = await client.get(f"/admin/registrations/{reg.registration_id}", cookies=acook)
    csrf = extract_csrf(resp.text)
    resp = await client.post(
        f"/admin/registrations/{reg.registration_id}/insurance/revoke",
        data={"csrf_token": csrf},
        cookies=acook,
    )
    assert resp.status_code == 303

    db.refresh(doc)
    assert doc.is_approved is False
    assert doc.approved_by is None
    assert doc.approved_at is None

    # Registration status unchanged
    db.refresh(reg)
    assert reg.status == "pending"


@pytest.mark.anyio
async def test_story21_insurance_covers_all_vendor_registrations(client, db, tmp_path):
    """Story 21: One insurance upload per email covers all registrations."""
    from app.main import app as fastapi_app
    fastapi_app.state.uploads_dir = tmp_path / "insurance"
    fastapi_app.state.uploads_dir.mkdir(parents=True, exist_ok=True)

    email = "shared@test.com"
    booths = seed_booth_types(db)
    reg1 = await register_vendor(client, db, email=email, booth_type_id=booths[0].id, business_name="Biz1")
    reg2 = await register_vendor(client, db, email=email, booth_type_id=booths[1].id, business_name="Biz2")

    vcook = vendor_cookie(email)

    # Upload insurance once
    resp = await client.get("/vendor/insurance", cookies=vcook)
    csrf = extract_csrf(resp.text)
    with patch("app.routes.vendor.send_admin_notification_email"):
        resp = await client.post(
            "/vendor/insurance/upload",
            data={"csrf_token": csrf},
            files={"file": ("ins.pdf", b"%PDF-1.4 test", "application/pdf")},
            cookies=vcook,
        )
    assert resp.status_code == 303

    # One document for the email
    docs = db.query(InsuranceDocument).filter(InsuranceDocument.email == email).all()
    assert len(docs) == 1

    # Both registration detail pages show the insurance doc
    for reg in [reg1, reg2]:
        resp = await client.get(
            f"/vendor/registration/{reg.registration_id}", cookies=vcook,
        )
        assert resp.status_code == 200
        assert "ins.pdf" in resp.text or "insurance" in resp.text.lower()


@pytest.mark.anyio
async def test_story22_rejected_vendor_resubmits(client, db):
    """Story 22: Rejected vendor submits a fresh registration."""
    email = "resub@test.com"
    reg1 = await register_vendor(client, db, email=email, business_name="First Biz")

    # Reject first registration
    seed_admin(db)
    acook = admin_cookie()
    resp = await client.get(f"/admin/registrations/{reg1.registration_id}", cookies=acook)
    csrf = extract_csrf(resp.text)
    with patch("app.routes.admin.send_rejection_email"):
        resp = await client.post(
            f"/admin/registrations/{reg1.registration_id}/reject",
            data={"csrf_token": csrf, "reversal_reason": "Rejected"},
            cookies=acook,
        )
    assert resp.status_code == 303
    db.refresh(reg1)
    assert reg1.status == "rejected"

    # Submit a fresh registration
    reg2 = await register_vendor(client, db, email=email, business_name="Second Biz")
    assert reg2.status == "pending"
    assert reg1.registration_id != reg2.registration_id

    # Both exist in the DB
    all_regs = db.query(Registration).filter(Registration.email == email).all()
    assert len(all_regs) == 2


@pytest.mark.anyio
async def test_story23_duplicate_webhook_idempotent(client, db):
    """Story 23: Duplicate webhook doesn't double-process."""
    reg = await register_vendor(client, db)
    reg = await approve_registration(client, db, reg.registration_id)
    reg = await pay_registration(client, db, reg.registration_id)
    assert reg.status == "paid"

    # Send duplicate webhook with the same event ID used by pay_registration
    event = build_webhook_event(
        f"evt_{reg.registration_id}",
        "payment_intent.succeeded",
        {"id": f"pi_test_{reg.registration_id}", "amount": reg.amount_paid},
    )
    with patch("app.routes.webhooks.stripe.Webhook.construct_event", return_value=event), \
         patch("app.routes.webhooks.send_payment_confirmation_email"):
        resp = await client.post(
            "/api/webhooks/stripe",
            content=json.dumps(event),
            headers={"stripe-signature": "test_sig"},
        )
    assert resp.status_code == 200
    assert "duplicate" in resp.json().get("status", "")

    # Only one stripe event record
    count = db.query(StripeEvent).filter(
        StripeEvent.stripe_event_id == f"evt_{reg.registration_id}",
    ).count()
    assert count == 1

    db.refresh(reg)
    assert reg.status == "paid"


@pytest.mark.anyio
async def test_story24_csv_export_all_statuses(client, db):
    """Story 24: CSV export reflects registrations at all five statuses."""
    seed_event_open(db)
    booths = seed_booth_types(db)
    bt_id = booths[0].id

    # Create one registration at each status
    reg_pending = await register_vendor(client, db, email="p@test.com", booth_type_id=bt_id, business_name="PendBiz")
    reg_approved = await register_vendor(client, db, email="a@test.com", booth_type_id=bt_id, business_name="AppBiz")
    reg_approved = await approve_registration(client, db, reg_approved.registration_id)

    reg_rejected = await register_vendor(client, db, email="r@test.com", booth_type_id=bt_id, business_name="RejBiz")
    seed_admin(db)
    acook = admin_cookie()
    resp = await client.get(f"/admin/registrations/{reg_rejected.registration_id}", cookies=acook)
    csrf = extract_csrf(resp.text)
    with patch("app.routes.admin.send_rejection_email"):
        await client.post(
            f"/admin/registrations/{reg_rejected.registration_id}/reject",
            data={"csrf_token": csrf, "reversal_reason": "Nope"},
            cookies=acook,
        )
    db.refresh(reg_rejected)

    reg_paid = await register_vendor(client, db, email="pd@test.com", booth_type_id=bt_id, business_name="PaidBiz")
    reg_paid = await approve_registration(client, db, reg_paid.registration_id)
    reg_paid = await pay_registration(client, db, reg_paid.registration_id)

    reg_cancelled = await register_vendor(client, db, email="c@test.com", booth_type_id=bt_id, business_name="CanBiz")
    reg_cancelled = await approve_registration(client, db, reg_cancelled.registration_id)
    reg_cancelled = await pay_registration(client, db, reg_cancelled.registration_id)
    reg_cancelled = await cancel_registration(client, db, reg_cancelled.registration_id)

    # Export CSV
    resp = await client.get("/admin/export", cookies=acook)
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]

    csv_text = resp.text
    lines = csv_text.strip().split("\n")
    # Header + 5 data rows
    assert len(lines) == 6, f"Expected 6 lines (header + 5 rows), got {len(lines)}"

    # All five statuses present
    for status in ["pending", "approved", "rejected", "paid", "cancelled"]:
        assert status in csv_text


@pytest.mark.anyio
async def test_story25_vendor_blocked_when_closed(client, db):
    """Story 25: Vendor blocked from registering when registration is closed."""
    seed_event_closed(db)
    seed_booth_types(db)
    vcook = vendor_cookie("closed@test.com")

    resp = await client.get("/vendor/register", cookies=vcook)
    assert resp.status_code == 200
    assert "closed" in resp.text.lower()


# ── Tier 5 — Security and Auth Boundaries ─────────────────────────────


@pytest.mark.anyio
async def test_story26_vendor_cant_access_admin(client, db):
    """Story 26: Vendor session can't access admin endpoints."""
    vcook = vendor_cookie("sneaky@test.com")

    resp = await client.get("/admin/registrations", cookies=vcook)
    assert resp.status_code in (303, 403)

    resp = await client.get("/admin", cookies=vcook)
    assert resp.status_code in (303, 403)


@pytest.mark.anyio
async def test_story27_admin_cant_impersonate_vendor(client, db):
    """Story 27: Admin session can't submit vendor registration."""
    seed_event_open(db)
    seed_booth_types(db)
    seed_admin(db)
    acook = admin_cookie()

    # Admin tries to access vendor registration form
    resp = await client.get("/vendor/register", cookies=acook)
    # Should redirect to login (admin is not a vendor)
    assert resp.status_code == 303 or "login" in resp.headers.get("location", "").lower()


@pytest.mark.anyio
async def test_story28_unauthenticated_redirected(client, db):
    """Story 28: Unauthenticated user redirected to login."""
    # No cookies at all
    for path in ["/vendor/dashboard", "/admin/registrations"]:
        resp = await client.get(path)
        assert resp.status_code == 303, f"{path} should redirect, got {resp.status_code}"
        assert "login" in resp.headers.get("location", "").lower() or \
               "auth" in resp.headers.get("location", "").lower()


@pytest.mark.anyio
async def test_story29_csrf_token_rejection(client, db):
    """Story 29: POST with missing or invalid CSRF token is rejected."""
    from app.csrf import generate_csrf_token

    seed_event_open(db)
    booths = seed_booth_types(db)
    reg = make_registration(db, booths[0].id, status="pending")
    seed_admin(db)
    acook = admin_cookie()

    # Missing CSRF token
    resp = await client.post(
        f"/admin/registrations/{reg.registration_id}/approve",
        data={},
        cookies=acook,
    )
    assert resp.status_code == 422  # FastAPI validation error (missing required form field)

    # Invalid CSRF token
    resp = await client.post(
        f"/admin/registrations/{reg.registration_id}/approve",
        data={"csrf_token": "totally-invalid-token"},
        cookies=acook,
    )
    assert resp.status_code == 403

    # Registration unchanged
    db.refresh(reg)
    assert reg.status == "pending"


@pytest.mark.anyio
async def test_story30_session_expiry(client, db):
    """Story 30: Expired sessions are rejected."""
    from app.session import _serializer

    # Vendor cookie with last_activity 5 hours ago (exceeds 4h inactivity timeout)
    vendor_data = {
        "user_type": "vendor",
        "email": "expired@test.com",
        "created_at": time.time(),
        "last_activity": time.time() - 5 * 3600,
    }
    expired_vcook = {"session": _serializer.dumps(vendor_data)}
    resp = await client.get("/vendor/dashboard", cookies=expired_vcook)
    assert resp.status_code == 303

    # Admin cookie with last_activity 2 hours ago (exceeds 1h inactivity timeout)
    seed_admin(db)
    admin_data = {
        "user_type": "admin",
        "email": "admin@test.com",
        "created_at": time.time(),
        "last_activity": time.time() - 2 * 3600,
    }
    expired_acook = {"session": _serializer.dumps(admin_data)}
    resp = await client.get("/admin/registrations", cookies=expired_acook)
    assert resp.status_code == 303


@pytest.mark.anyio
async def test_story31_otp_rate_limiting(client, db):
    """Story 31: Excessive OTP requests from same email are rate-limited."""
    from app.csrf import generate_csrf_token
    from app.routes.auth import _otp_ip_counts

    # Clear any existing rate limit state
    _otp_ip_counts.clear()

    seed_event_open(db)
    email = "ratelimit@test.com"

    # Send 5 OTP requests (should all succeed at per-email level)
    for i in range(5):
        csrf = generate_csrf_token()
        with patch("app.routes.auth.send_otp_email", return_value=True):
            resp = await client.post(
                "/auth/login",
                data={"csrf_token": csrf, "email": email, "role": "vendor"},
            )
        assert resp.status_code == 200, f"Request {i+1} failed with {resp.status_code}"

    # 6th request should be rate-limited (per-email: max 5 per hour)
    csrf = generate_csrf_token()
    with patch("app.routes.auth.send_otp_email", return_value=True):
        resp = await client.post(
            "/auth/login",
            data={"csrf_token": csrf, "email": email, "role": "vendor"},
        )
    assert resp.status_code == 429


@pytest.mark.anyio
async def test_story33_invalid_webhook_signature(client, db):
    """Story 33: Webhook with invalid signature is rejected."""
    import stripe as stripe_lib

    event_data = {"id": "evt_bad", "type": "payment_intent.succeeded",
                  "data": {"object": {"id": "pi_bad", "amount": 5000}}}

    # Raise SignatureVerificationError (what Stripe raises on bad sig)
    with patch("app.routes.webhooks.stripe.Webhook.construct_event",
               side_effect=stripe_lib.SignatureVerificationError("bad", "sig")):
        resp = await client.post(
            "/api/webhooks/stripe",
            content=json.dumps(event_data),
            headers={"stripe-signature": "bad_sig"},
        )
    assert resp.status_code == 400
