"""End-to-end user story tests."""

import pytest
from tests.helpers import register_vendor, approve_registration, pay_registration


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
