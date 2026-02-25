"""Tests for insurance document upload, download, and admin approval."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import InsuranceDocument
from tests.helpers import (
    admin_cookie, vendor_cookie, extract_csrf,
    seed_admin, seed_booth_types, seed_event, make_registration, make_insurance_doc,
)


@pytest.fixture(autouse=True)
def _setup_uploads_dir(tmp_path):
    """Point app.state.uploads_dir to a temp directory for tests."""
    app.state.uploads_dir = tmp_path / "insurance"
    app.state.uploads_dir.mkdir(parents=True, exist_ok=True)
    yield


def _pdf_bytes():
    """Minimal PDF-like bytes for testing."""
    return b"%PDF-1.4 fake content for testing"


# ========================================
# Vendor: insurance page
# ========================================

@pytest.mark.anyio
async def test_insurance_page_no_doc(db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/insurance", cookies=vendor_cookie())
        assert response.status_code == 200
        assert "No document uploaded" in response.text
        assert "Upload Document" in response.text


@pytest.mark.anyio
async def test_insurance_page_with_uploaded_doc(db):
    make_insurance_doc(db, email="vendor@test.com", approved=False)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/insurance", cookies=vendor_cookie())
        assert response.status_code == 200
        assert "Pending Review" in response.text
        assert "insurance.pdf" in response.text


@pytest.mark.anyio
async def test_insurance_page_with_approved_doc(db):
    make_insurance_doc(db, email="vendor@test.com", approved=True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/insurance", cookies=vendor_cookie())
        assert response.status_code == 200
        assert "Approved" in response.text


# ========================================
# Vendor: upload
# ========================================

@pytest.mark.anyio
async def test_upload_pdf(db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        page = await client.get("/vendor/insurance", cookies=vendor_cookie())
        csrf = extract_csrf(page.text)

        response = await client.post("/vendor/insurance/upload",
            cookies=vendor_cookie(),
            data={"csrf_token": csrf},
            files={"file": ("cert.pdf", _pdf_bytes(), "application/pdf")},
        )
        assert response.status_code == 303

    doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == "vendor@test.com").first()
    assert doc is not None
    assert doc.original_filename == "cert.pdf"
    assert doc.content_type == "application/pdf"
    assert doc.is_approved is False
    # File should exist on disk
    assert (app.state.uploads_dir / doc.stored_filename).exists()


@pytest.mark.anyio
async def test_reupload_replaces_old_file(db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        # First upload
        page = await client.get("/vendor/insurance", cookies=vendor_cookie())
        csrf = extract_csrf(page.text)
        await client.post("/vendor/insurance/upload",
            cookies=vendor_cookie(),
            data={"csrf_token": csrf},
            files={"file": ("first.pdf", _pdf_bytes(), "application/pdf")},
        )

    doc1 = db.query(InsuranceDocument).filter(InsuranceDocument.email == "vendor@test.com").first()
    first_stored = doc1.stored_filename

    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        # Second upload
        page = await client.get("/vendor/insurance", cookies=vendor_cookie())
        csrf = extract_csrf(page.text)
        await client.post("/vendor/insurance/upload",
            cookies=vendor_cookie(),
            data={"csrf_token": csrf},
            files={"file": ("second.pdf", _pdf_bytes(), "application/pdf")},
        )

    db.expire_all()
    doc2 = db.query(InsuranceDocument).filter(InsuranceDocument.email == "vendor@test.com").first()
    assert doc2.original_filename == "second.pdf"
    assert doc2.stored_filename != first_stored
    # Old file should be deleted
    assert not (app.state.uploads_dir / first_stored).exists()
    # New file should exist
    assert (app.state.uploads_dir / doc2.stored_filename).exists()
    # Still only one record
    assert db.query(InsuranceDocument).filter(InsuranceDocument.email == "vendor@test.com").count() == 1


@pytest.mark.anyio
async def test_upload_invalid_extension(db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/vendor/insurance", cookies=vendor_cookie())
        csrf = extract_csrf(page.text)

        response = await client.post("/vendor/insurance/upload",
            cookies=vendor_cookie(),
            data={"csrf_token": csrf},
            files={"file": ("virus.exe", b"bad content", "application/octet-stream")},
        )
        assert response.status_code == 200
        assert "File type not allowed" in response.text

    assert db.query(InsuranceDocument).filter(InsuranceDocument.email == "vendor@test.com").first() is None


@pytest.mark.anyio
async def test_upload_oversized_file(db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/vendor/insurance", cookies=vendor_cookie())
        csrf = extract_csrf(page.text)

        big_content = b"x" * (10 * 1024 * 1024 + 1)
        response = await client.post("/vendor/insurance/upload",
            cookies=vendor_cookie(),
            data={"csrf_token": csrf},
            files={"file": ("big.pdf", big_content, "application/pdf")},
        )
        assert response.status_code == 200
        assert "too large" in response.text

    assert db.query(InsuranceDocument).filter(InsuranceDocument.email == "vendor@test.com").first() is None


# ========================================
# Vendor: download
# ========================================

@pytest.mark.anyio
async def test_download_own_file(db):
    doc = make_insurance_doc(db, email="vendor@test.com", stored_filename="test123.pdf")
    # Write a file on disk
    file_path = app.state.uploads_dir / "test123.pdf"
    file_path.write_bytes(_pdf_bytes())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/insurance/file/test123.pdf", cookies=vendor_cookie())
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"


@pytest.mark.anyio
async def test_download_other_vendor_file_denied(db):
    doc = make_insurance_doc(db, email="other@test.com", stored_filename="other456.pdf")
    file_path = app.state.uploads_dir / "other456.pdf"
    file_path.write_bytes(_pdf_bytes())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get("/vendor/insurance/file/other456.pdf", cookies=vendor_cookie())
        assert response.status_code == 303  # Redirect, not served


# ========================================
# Admin: download
# ========================================

@pytest.mark.anyio
async def test_admin_download_file(db):
    seed_admin(db)
    doc = make_insurance_doc(db, email="vendor@test.com", stored_filename="adm789.pdf")
    file_path = app.state.uploads_dir / "adm789.pdf"
    file_path.write_bytes(_pdf_bytes())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/insurance/adm789.pdf", cookies=admin_cookie())
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"


# ========================================
# Admin: approve / revoke
# ========================================

@pytest.mark.anyio
async def test_admin_approve_insurance(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, reg_id="ANM-2026-0090", email="vendor@test.com")
    make_insurance_doc(db, email="vendor@test.com", approved=False, stored_filename="ins1.pdf")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        detail = await client.get("/admin/registrations/ANM-2026-0090", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        response = await client.post("/admin/registrations/ANM-2026-0090/insurance/approve",
            data={"csrf_token": csrf},
            cookies=admin_cookie(),
        )
        assert response.status_code == 303

    doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == "vendor@test.com").first()
    db.refresh(doc)
    assert doc.is_approved is True
    assert doc.approved_by == "admin@test.com"
    assert doc.approved_at is not None


@pytest.mark.anyio
async def test_admin_revoke_insurance(db):
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, reg_id="ANM-2026-0091", email="vendor@test.com")
    make_insurance_doc(db, email="vendor@test.com", approved=True, stored_filename="ins2.pdf")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        detail = await client.get("/admin/registrations/ANM-2026-0091", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        response = await client.post("/admin/registrations/ANM-2026-0091/insurance/revoke",
            data={"csrf_token": csrf},
            cookies=admin_cookie(),
        )
        assert response.status_code == 303

    doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == "vendor@test.com").first()
    db.refresh(doc)
    assert doc.is_approved is False
    assert doc.approved_by is None
    assert doc.approved_at is None


@pytest.mark.anyio
async def test_admin_approve_no_doc_redirects(db):
    """Approve with no uploaded document just redirects."""
    seed_admin(db)
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, reg_id="ANM-2026-0092", email="vendor@test.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        detail = await client.get("/admin/registrations/ANM-2026-0092", cookies=admin_cookie())
        csrf = extract_csrf(detail.text)

        response = await client.post("/admin/registrations/ANM-2026-0092/insurance/approve",
            data={"csrf_token": csrf},
            cookies=admin_cookie(),
        )
        assert response.status_code == 303


# ========================================
# Dashboard shows insurance status
# ========================================

@pytest.mark.anyio
async def test_vendor_dashboard_shows_insurance_status(db):
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, reg_id="ANM-2026-0095", email="vendor@test.com")
    make_insurance_doc(db, email="vendor@test.com", approved=False, stored_filename="dash1.pdf")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/dashboard", cookies=vendor_cookie())
        assert response.status_code == 200
        assert "Pending Review" in response.text
        assert "/vendor/insurance" in response.text


@pytest.mark.anyio
async def test_vendor_dashboard_shows_upload_required(db):
    booths = seed_booth_types(db)
    make_registration(db, booths[0].id, reg_id="ANM-2026-0096", email="vendor@test.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/vendor/dashboard", cookies=vendor_cookie())
        assert response.status_code == 200
        assert "Insurance document required" in response.text
