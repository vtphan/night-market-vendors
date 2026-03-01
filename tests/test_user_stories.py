"""End-to-end user story tests."""

import pytest
from unittest.mock import patch

from app.models import Registration, InsuranceDocument
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
