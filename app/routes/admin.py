import csv
import io
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.csrf import generate_csrf_token, require_csrf
from app.session import require_admin
from app.models import Registration, BoothType, EventSettings
from app.services.registration import (
    transition_status,
    get_inventory,
)
from app.services.email import send_approval_email, send_rejection_email, send_refund_email
from app.services.payment import create_refund
from app.config import APP_URL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _template(request, name, ctx, session=None):
    """Render a template with standard context."""
    ctx.setdefault("request", request)
    ctx.setdefault("session", session)
    ctx.setdefault("csrf_token", generate_csrf_token())
    ctx.setdefault("get_flashed_messages", lambda: [])
    return request.app.state.templates.TemplateResponse(name, ctx)


# --- Dashboard ---

@router.get("", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # Counts by status
    statuses = ["pending", "approved", "rejected", "confirmed", "cancelled"]
    counts = {}
    for s in statuses:
        counts[s] = db.query(Registration).filter(Registration.status == s).count()
    counts["total"] = sum(counts.values())

    inventory = get_inventory(db)

    return _template(request, "admin/dashboard.html", {
        "counts": counts,
        "inventory": inventory,
    }, session=session)


# --- Registration list ---

@router.get("/registrations", response_class=HTMLResponse)
async def registration_list(
    request: Request,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    status: str = Query("", alias="status"),
    category: str = Query("", alias="category"),
    search: str = Query("", alias="search"),
):
    query = db.query(Registration)

    if status:
        query = query.filter(Registration.status == status)
    if category:
        query = query.filter(Registration.category == category)
    if search:
        term = f"%{search}%"
        query = query.filter(
            (Registration.business_name.ilike(term))
            | (Registration.contact_name.ilike(term))
            | (Registration.email.ilike(term))
            | (Registration.registration_id.ilike(term))
        )

    registrations = query.order_by(Registration.created_at.desc()).all()

    booth_types = {bt.id: bt for bt in db.query(BoothType).all()}

    return _template(request, "admin/registrations.html", {
        "registrations": registrations,
        "booth_types": booth_types,
        "filter_status": status,
        "filter_category": category,
        "filter_search": search,
    }, session=session)


# --- Registration detail ---

@router.get("/registrations/{reg_id}", response_class=HTMLResponse)
async def registration_detail(
    request: Request,
    reg_id: str,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == reg_id)
        .first()
    )
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()

    return _template(request, "admin/registration_detail.html", {
        "registration": registration,
        "booth_type": booth_type,
    }, session=session)


# --- Approve registration ---

@router.post("/registrations/{reg_id}/approve")
async def approve_registration(
    request: Request,
    reg_id: str,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == reg_id)
        .first()
    )
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    try:
        transition_status(db, registration, "approved")
    except ValueError as e:
        logger.warning("Invalid transition for %s: %s", reg_id, e)
        booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()
        flash = [{"category": "error", "text": f"Cannot approve: {e}"}]
        return _template(request, "admin/registration_detail.html", {
            "registration": registration,
            "booth_type": booth_type,
            "get_flashed_messages": lambda: flash,
        }, session=session)

    payment_url = f"{APP_URL}/vendor/registration/{reg_id}"
    send_approval_email(registration.email, reg_id, payment_url)

    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Reject registration ---

@router.post("/registrations/{reg_id}/reject")
async def reject_registration(
    request: Request,
    reg_id: str,
    rejection_reason: str = Form(""),
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == reg_id)
        .first()
    )
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    try:
        transition_status(db, registration, "rejected", rejection_reason=rejection_reason or None)
    except ValueError as e:
        logger.warning("Invalid transition for %s: %s", reg_id, e)
        booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()
        flash = [{"category": "error", "text": f"Cannot reject: {e}"}]
        return _template(request, "admin/registration_detail.html", {
            "registration": registration,
            "booth_type": booth_type,
            "get_flashed_messages": lambda: flash,
        }, session=session)

    send_rejection_email(registration.email, reg_id, rejection_reason or None)

    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Unreject registration (back to pending) ---

@router.post("/registrations/{reg_id}/unreject")
async def unreject_registration(
    request: Request,
    reg_id: str,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == reg_id)
        .first()
    )
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    try:
        transition_status(db, registration, "pending")
    except ValueError as e:
        logger.warning("Invalid transition for %s: %s", reg_id, e)
        booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()
        flash = [{"category": "error", "text": f"Cannot unreject: {e}"}]
        return _template(request, "admin/registration_detail.html", {
            "registration": registration,
            "booth_type": booth_type,
            "get_flashed_messages": lambda: flash,
        }, session=session)

    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Cancel + Refund ---

@router.post("/registrations/{reg_id}/cancel")
async def cancel_registration(
    request: Request,
    reg_id: str,
    refund_amount: str = Form("0"),
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == reg_id)
        .first()
    )
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    if registration.status != "confirmed":
        logger.warning("Cannot cancel %s: status is %s", reg_id, registration.status)
        return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)

    # Convert dollar amount to cents
    try:
        amount_cents = int(float(refund_amount) * 100)
    except (ValueError, TypeError):
        amount_cents = 0

    if amount_cents > 0 and registration.stripe_payment_intent_id:
        try:
            create_refund(db, registration, amount_cents)
        except Exception:
            logger.exception("Stripe refund failed for %s", reg_id)
            booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()
            flash = [{"category": "error", "text": "Refund failed. Please check Stripe and try again."}]
            return _template(request, "admin/registration_detail.html", {
                "registration": registration,
                "booth_type": booth_type,
                "get_flashed_messages": lambda: flash,
            }, session=session)

    try:
        transition_status(db, registration, "cancelled")
    except ValueError as e:
        logger.warning("Invalid transition for %s: %s", reg_id, e)
        return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)

    send_refund_email(registration.email, reg_id, amount_cents)

    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Update registration fields ---

@router.post("/registrations/{reg_id}/update")
async def update_registration(
    request: Request,
    reg_id: str,
    documents_approved: str = Form(""),
    category: str = Form(""),
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == reg_id)
        .first()
    )
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    registration.documents_approved = documents_approved == "on"
    if category in ("food", "non_food"):
        registration.category = category

    db.commit()
    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Inventory ---

@router.get("/inventory", response_class=HTMLResponse)
async def inventory_page(
    request: Request,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    inventory = get_inventory(db)
    return _template(request, "admin/inventory.html", {
        "inventory": inventory,
    }, session=session)


@router.post("/inventory/{booth_type_id}")
async def update_inventory(
    request: Request,
    booth_type_id: int,
    total_quantity: int = Form(...),
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    booth_type = db.query(BoothType).filter(BoothType.id == booth_type_id).first()
    if booth_type and total_quantity >= 0:
        booth_type.total_quantity = total_quantity
        db.commit()
    return RedirectResponse(url="/admin/inventory", status_code=303)


# --- Settings ---

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    settings = db.query(EventSettings).first()
    return _template(request, "admin/settings.html", {
        "settings": settings,
    }, session=session)


@router.post("/settings")
async def update_settings(
    request: Request,
    registration_open_date: str = Form(...),
    registration_close_date: str = Form(...),
    front_page_content: str = Form(""),
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    settings = db.query(EventSettings).first()
    if settings:
        try:
            settings.registration_open_date = datetime.fromisoformat(registration_open_date)
            settings.registration_close_date = datetime.fromisoformat(registration_close_date)
            settings.front_page_content = front_page_content.strip()
            db.commit()
        except ValueError:
            pass
    return RedirectResponse(url="/admin/settings", status_code=303)


# --- CSV Export ---

@router.get("/export")
async def export_csv(
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    registrations = db.query(Registration).order_by(Registration.created_at.desc()).all()
    booth_types = {bt.id: bt.name for bt in db.query(BoothType).all()}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Registration ID", "Status", "Business Name", "Contact Name",
        "Email", "Phone", "Category", "Cuisine Type", "Description",
        "Booth Type", "Needs Power", "Needs Water", "Needs Propane",
        "Documents Approved", "Amount Paid", "Refund Amount",
        "Stripe Payment Intent ID", "Created At", "Approved At",
        "Rejected At", "Rejection Reason",
    ])

    for reg in registrations:
        writer.writerow([
            reg.registration_id,
            reg.status,
            reg.business_name,
            reg.contact_name,
            reg.email,
            reg.phone,
            reg.category,
            reg.cuisine_type or "",
            reg.description,
            booth_types.get(reg.booth_type_id, "Unknown"),
            "Yes" if reg.needs_power else "No",
            "Yes" if reg.needs_water else "No",
            "Yes" if reg.needs_propane else "No",
            "Yes" if reg.documents_approved else "No",
            f"${reg.amount_paid / 100:.2f}" if reg.amount_paid else "",
            f"${reg.refund_amount / 100:.2f}" if reg.refund_amount else "",
            reg.stripe_payment_intent_id or "",
            reg.created_at.strftime("%Y-%m-%d %H:%M") if reg.created_at else "",
            reg.approved_at.strftime("%Y-%m-%d %H:%M") if reg.approved_at else "",
            reg.rejected_at.strftime("%Y-%m-%d %H:%M") if reg.rejected_at else "",
            reg.rejection_reason or "",
        ])

    output.seek(0)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=registrations_{timestamp}.csv"
        },
    )
