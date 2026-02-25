"""Tests for registration service: status transitions, ID generation, rate limiting, inventory."""

import time
from datetime import datetime, timezone

import pytest

from app.models import Registration, BoothType
from app.services.registration import (
    VALID_TRANSITIONS,
    transition_status,
    generate_registration_id,
    create_registration,
    check_submission_rate_limit,
    reset_rate_limits,
    get_inventory,
)


# --- Helpers ---

def _make_booth_type(db, name="Regular Booth", price=10000, qty=80) -> BoothType:
    bt = BoothType(name=name, description="Test booth", total_quantity=qty, price=price, sort_order=1)
    db.add(bt)
    db.commit()
    db.refresh(bt)
    return bt


def _make_registration(db, booth_type_id, status="pending", email="vendor@test.com", reg_id=None) -> Registration:
    if reg_id is None:
        reg_id = generate_registration_id(db)
    reg = Registration(
        registration_id=reg_id,
        email=email,
        business_name="Test Biz",
        contact_name="Test Vendor",
        phone="555-0100",
        category="food",
        description="Test food booth",
        booth_type_id=booth_type_id,
        status=status,
        agreement_accepted_at=datetime.now(timezone.utc),
        agreement_ip_address="127.0.0.1",
    )
    db.add(reg)
    db.commit()
    db.refresh(reg)
    return reg


# --- Valid transitions ---

def test_pending_to_approved(db):
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="pending")
    result = transition_status(db, reg, "approved")
    assert result.status == "approved"
    assert result.approved_at is not None


def test_pending_to_rejected(db):
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="pending")
    result = transition_status(db, reg, "rejected", rejection_reason="Does not meet criteria")
    assert result.status == "rejected"
    assert result.rejected_at is not None
    assert result.rejection_reason == "Does not meet criteria"


def test_approved_to_rejected(db):
    """Admin can revoke approval before payment."""
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="approved")
    result = transition_status(db, reg, "rejected")
    assert result.status == "rejected"
    assert result.rejected_at is not None


def test_approved_to_paid(db):
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="approved")
    result = transition_status(db, reg, "paid")
    assert result.status == "paid"


def test_paid_to_cancelled(db):
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="paid")
    result = transition_status(db, reg, "cancelled")
    assert result.status == "cancelled"


# --- Invalid transitions ---

def test_pending_to_paid_invalid(db):
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="pending")
    with pytest.raises(ValueError, match="Cannot transition"):
        transition_status(db, reg, "paid")


def test_pending_to_cancelled_invalid(db):
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="pending")
    with pytest.raises(ValueError, match="Cannot transition"):
        transition_status(db, reg, "cancelled")


def test_rejected_to_pending(db):
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="rejected")
    reg.rejected_at = datetime.now(timezone.utc)
    reg.rejection_reason = "Some reason"
    db.commit()
    transition_status(db, reg, "pending")
    assert reg.status == "pending"
    assert reg.rejected_at is None
    assert reg.rejection_reason is None


def test_rejected_to_other_invalid(db):
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="rejected")
    for target in ["approved", "paid", "cancelled"]:
        with pytest.raises(ValueError, match="Cannot transition"):
            transition_status(db, reg, target)


def test_cancelled_to_any_invalid(db):
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="cancelled")
    for target in ["pending", "approved", "rejected", "paid"]:
        with pytest.raises(ValueError, match="Cannot transition"):
            transition_status(db, reg, target)


def test_approved_to_pending(db):
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="approved")
    reg.approved_at = datetime.now(timezone.utc)
    db.commit()
    transition_status(db, reg, "pending")
    assert reg.status == "pending"
    assert reg.approved_at is None


# --- Timestamp tests ---

def test_approve_sets_approved_at(db):
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="pending")
    assert reg.approved_at is None
    transition_status(db, reg, "approved")
    assert reg.approved_at is not None


def test_reject_sets_rejected_at_and_reason(db):
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="pending")
    assert reg.rejected_at is None
    transition_status(db, reg, "rejected", rejection_reason="Not a good fit")
    assert reg.rejected_at is not None
    assert reg.rejection_reason == "Not a good fit"


def test_documents_approved_does_not_affect_status(db):
    """documents_approved is informational only — changing it doesn't transition status."""
    bt = _make_booth_type(db)
    reg = _make_registration(db, bt.id, status="pending")
    reg.documents_approved = True
    db.commit()
    db.refresh(reg)
    assert reg.status == "pending"
    assert reg.documents_approved is True


# --- Registration ID ---

def test_registration_id_format(db):
    bt = _make_booth_type(db)
    reg_id = generate_registration_id(db)
    year = datetime.now(timezone.utc).year
    assert reg_id == f"ANM-{year}-0001"


def test_registration_id_auto_increment(db):
    bt = _make_booth_type(db)
    _make_registration(db, bt.id)
    second_id = generate_registration_id(db)
    year = datetime.now(timezone.utc).year
    assert second_id == f"ANM-{year}-0002"


def test_create_registration_generates_id(db):
    bt = _make_booth_type(db)
    data = {
        "email": "new@vendor.com",
        "business_name": "New Biz",
        "contact_name": "New Vendor",
        "phone": "555-0200",
        "category": "merchandise",
        "description": "Handmade crafts",
        "booth_type_id": bt.id,
        "agreement_accepted_at": datetime.now(timezone.utc),
        "agreement_ip_address": "10.0.0.1",
    }
    reg = create_registration(db, data)
    assert reg.registration_id.startswith("ANM-")
    assert reg.status == "pending"


# --- Rate limiting ---

def test_rate_limit_allows_10(db):
    reset_rate_limits()
    for i in range(10):
        assert check_submission_rate_limit("192.168.1.1") is True


def test_rate_limit_blocks_11th(db):
    reset_rate_limits()
    for i in range(10):
        check_submission_rate_limit("192.168.1.2")
    assert check_submission_rate_limit("192.168.1.2") is False


def test_rate_limit_different_ips_independent(db):
    reset_rate_limits()
    for i in range(10):
        check_submission_rate_limit("10.0.0.1")
    assert check_submission_rate_limit("10.0.0.1") is False
    assert check_submission_rate_limit("10.0.0.2") is True


# --- Inventory ---

def test_inventory_empty(db):
    bt = _make_booth_type(db, qty=50)
    inventory = get_inventory(db)
    assert len(inventory) == 1
    assert inventory[0]["available"] == 50
    assert inventory[0]["reserved"] == 0


def test_inventory_counts_approved_and_paid(db):
    bt = _make_booth_type(db, qty=10)
    _make_registration(db, bt.id, status="approved", email="a@test.com", reg_id="ANM-2026-0001")
    _make_registration(db, bt.id, status="approved", email="b@test.com", reg_id="ANM-2026-0002")
    _make_registration(db, bt.id, status="paid", email="c@test.com", reg_id="ANM-2026-0003")
    _make_registration(db, bt.id, status="pending", email="d@test.com", reg_id="ANM-2026-0004")
    _make_registration(db, bt.id, status="rejected", email="e@test.com", reg_id="ANM-2026-0005")

    inventory = get_inventory(db)
    assert inventory[0]["approved"] == 2
    assert inventory[0]["paid"] == 1
    assert inventory[0]["reserved"] == 3
    assert inventory[0]["available"] == 7  # 10 - 3


