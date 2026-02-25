import csv
import io
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse
from sqlalchemy import func as sa_func, extract
from sqlalchemy.orm import Session

from app.database import get_db
from app.csrf import generate_csrf_token, require_csrf
from app.session import require_admin
from app.models import Registration, BoothType, EventSettings, InsuranceDocument
from app.services.registration import (
    transition_status,
    approve_with_inventory_check,
    get_inventory,
    get_booth_availability,
    LOW_INVENTORY_THRESHOLD,
    CATEGORIES,
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
    statuses = ["pending", "approved", "rejected", "paid", "cancelled"]
    counts = {}
    for s in statuses:
        counts[s] = db.query(Registration).filter(Registration.status == s).count()
    counts["total"] = sum(counts.values())

    inventory = get_inventory(db)

    # Insurance counts (per unique vendor email with active registrations)
    active_emails_q = (
        db.query(Registration.email)
        .filter(Registration.status.in_(["pending", "approved", "paid"]))
        .distinct()
    )
    active_email_list = [r[0] for r in active_emails_q.all()]
    all_docs = {
        doc.email: doc
        for doc in db.query(InsuranceDocument)
        .filter(InsuranceDocument.email.in_(active_email_list))
        .all()
    }
    insurance_counts = {
        "approved": sum(1 for e in active_email_list if e in all_docs and all_docs[e].is_approved),
        "uploaded": sum(1 for e in active_email_list if e in all_docs and not all_docs[e].is_approved),
        "none": sum(1 for e in active_email_list if e not in all_docs),
    }

    notes_count = db.query(Registration).filter(
        sa_func.length(sa_func.trim(Registration.admin_notes)) > 0
    ).count()

    # Revenue: total paid amount
    revenue_total = db.query(sa_func.coalesce(sa_func.sum(Registration.amount_paid), 0)).filter(
        Registration.status == "paid"
    ).scalar() or 0
    refund_total = db.query(sa_func.coalesce(sa_func.sum(Registration.refund_amount), 0)).filter(
        Registration.status.in_(["paid", "cancelled"])
    ).scalar() or 0

    # Revenue by booth type
    revenue_by_booth = (
        db.query(BoothType.name, sa_func.sum(Registration.amount_paid))
        .join(BoothType, Registration.booth_type_id == BoothType.id)
        .filter(Registration.status == "paid")
        .group_by(BoothType.name)
        .all()
    )

    # Recent pending registrations (up to 5)
    recent_pending = (
        db.query(Registration)
        .filter(Registration.status == "pending")
        .order_by(Registration.created_at.desc())
        .limit(5)
        .all()
    )

    # Last registration timestamp
    last_registration = (
        db.query(Registration.created_at)
        .order_by(Registration.created_at.desc())
        .first()
    )
    last_registration_at = last_registration[0] if last_registration else None

    # Registration time distribution (last 30 days, by day)
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    daily_counts_raw = (
        db.query(
            sa_func.date(Registration.created_at).label("day"),
            sa_func.count(Registration.id),
        )
        .filter(Registration.created_at >= thirty_days_ago)
        .group_by("day")
        .order_by("day")
        .all()
    )
    # Fill in missing days with 0
    daily_counts = []
    if daily_counts_raw:
        day_map = {row[0]: row[1] for row in daily_counts_raw}
        start_date = thirty_days_ago.date()
        end_date = datetime.now(timezone.utc).date()
        current = start_date
        while current <= end_date:
            daily_counts.append({
                "date": current.strftime("%b %d"),
                "count": day_map.get(str(current), 0),
            })
            current += timedelta(days=1)

    # Hourly distribution (all time)
    hourly_counts_raw = (
        db.query(
            extract("hour", Registration.created_at).label("hour"),
            sa_func.count(Registration.id),
        )
        .group_by("hour")
        .order_by("hour")
        .all()
    )
    hourly_counts = [0] * 24
    for row in hourly_counts_raw:
        if row[0] is not None:
            hourly_counts[int(row[0])] = row[1]

    # Capacity alerts: booth types where pending registrations meet or exceed available slots
    capacity_alerts = []
    for item in inventory:
        if item["pending"] > 0 and item["available"] <= item["pending"]:
            overflow = item["pending"] - item["available"]
            capacity_alerts.append({
                "id": item["id"],
                "name": item["name"],
                "available": item["available"],
                "pending": item["pending"],
                "overflow": overflow,  # how many pending can't fit
                "total_quantity": item["total_quantity"],
                "reserved": item["reserved"],
            })

    # Insurance docs pending review (up to 5)
    pending_insurance = []
    emails_with_pending_docs = (
        db.query(InsuranceDocument)
        .filter(InsuranceDocument.is_approved == False)
        .limit(5)
        .all()
    )
    for doc in emails_with_pending_docs:
        reg = db.query(Registration).filter(
            Registration.email == doc.email,
            Registration.status.in_(["pending", "approved", "paid"]),
        ).first()
        if reg:
            pending_insurance.append({"doc": doc, "registration": reg})

    return _template(request, "admin/dashboard.html", {
        "counts": counts,
        "inventory": inventory,
        "insurance_counts": insurance_counts,
        "notes_count": notes_count,
        "revenue_total": revenue_total,
        "refund_total": refund_total,
        "revenue_by_booth": revenue_by_booth,
        "recent_pending": recent_pending,
        "last_registration_at": last_registration_at,
        "daily_counts": daily_counts,
        "hourly_counts": hourly_counts,
        "pending_insurance": pending_insurance,
        "capacity_alerts": capacity_alerts,
    }, session=session)


# --- Registration list ---

@router.get("/registrations", response_class=HTMLResponse)
async def registration_list(
    request: Request,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    status: str = Query("", alias="status"),
    category: str = Query("", alias="category"),
    booth_type: str = Query("", alias="booth_type"),
    insurance: str = Query("", alias="insurance"),
    notes: str = Query("", alias="notes"),
    search: str = Query("", alias="search"),
):
    query = db.query(Registration)

    if status:
        query = query.filter(Registration.status == status)
    if category:
        query = query.filter(Registration.category == category)
    if booth_type:
        try:
            query = query.filter(Registration.booth_type_id == int(booth_type))
        except ValueError:
            pass
    if insurance == "approved":
        emails_approved = db.query(InsuranceDocument.email).filter(InsuranceDocument.is_approved == True).subquery()
        query = query.filter(Registration.email.in_(emails_approved))
    elif insurance == "uploaded":
        emails_uploaded = db.query(InsuranceDocument.email).filter(InsuranceDocument.is_approved == False).subquery()
        query = query.filter(Registration.email.in_(emails_uploaded))
    elif insurance == "no":
        emails_with_doc = db.query(InsuranceDocument.email).subquery()
        query = query.filter(~Registration.email.in_(emails_with_doc))
    if notes == "yes":
        query = query.filter(sa_func.length(sa_func.trim(Registration.admin_notes)) > 0)
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

    # Build insurance doc lookup by email
    insurance_docs = {doc.email: doc for doc in db.query(InsuranceDocument).all()}

    return _template(request, "admin/registrations.html", {
        "registrations": registrations,
        "booth_types": booth_types,
        "insurance_docs": insurance_docs,
        "filter_status": status,
        "filter_category": category,
        "filter_booth_type": booth_type,
        "filter_insurance": insurance,
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
    available = get_booth_availability(db, registration.booth_type_id)
    insurance_doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == registration.email).first()

    return _template(request, "admin/registration_detail.html", {
        "registration": registration,
        "booth_type": booth_type,
        "booth_availability": available,
        "LOW_INVENTORY_THRESHOLD": LOW_INVENTORY_THRESHOLD,
        "insurance_doc": insurance_doc,
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
        approve_with_inventory_check(db, registration)
    except ValueError as e:
        logger.warning("Cannot approve %s: %s", reg_id, e)
        booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()
        available = get_booth_availability(db, registration.booth_type_id)
        flash = [{"category": "error", "text": f"Cannot approve: {e}"}]
        return _template(request, "admin/registration_detail.html", {
            "registration": registration,
            "booth_type": booth_type,
            "booth_availability": available,
            "LOW_INVENTORY_THRESHOLD": LOW_INVENTORY_THRESHOLD,
            "get_flashed_messages": lambda: flash,
        }, session=session)

    payment_url = f"{APP_URL}/vendor/registration/{reg_id}"
    settings = db.query(EventSettings).first()
    send_approval_email(
        registration.email, reg_id, payment_url,
        insurance_instructions=settings.insurance_instructions if settings else "",
    )

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

    rejection_reason = rejection_reason.strip()
    if not rejection_reason:
        booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()
        flash = [{"category": "error", "text": "A rejection reason is required."}]
        return _template(request, "admin/registration_detail.html", {
            "registration": registration,
            "booth_type": booth_type,
            "get_flashed_messages": lambda: flash,
        }, session=session)

    try:
        transition_status(db, registration, "rejected", rejection_reason=rejection_reason)
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

    if registration.status != "paid":
        logger.warning("Cannot cancel %s: status is %s", reg_id, registration.status)
        return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)

    # Convert dollar amount to cents
    try:
        amount_cents = int(float(refund_amount) * 100)
        if amount_cents < 0:
            amount_cents = 0
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

    if category in CATEGORIES:
        registration.category = category

    db.commit()
    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Admin Notes ---

@router.post("/registrations/{reg_id}/notes")
async def update_notes(
    request: Request,
    reg_id: str,
    admin_notes: str = Form(""),
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

    registration.admin_notes = admin_notes.strip() or None
    db.commit()
    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Insurance ---

@router.get("/insurance/{stored_filename}")
async def admin_insurance_file(
    request: Request,
    stored_filename: str,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    doc = db.query(InsuranceDocument).filter(InsuranceDocument.stored_filename == stored_filename).first()
    if not doc:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    file_path = request.app.state.uploads_dir / stored_filename
    if not file_path.exists():
        return RedirectResponse(url="/admin/registrations", status_code=303)

    return FileResponse(
        path=str(file_path),
        media_type=doc.content_type,
        filename=doc.original_filename,
    )


@router.post("/registrations/{reg_id}/insurance/approve")
async def toggle_insurance_approval(
    request: Request,
    reg_id: str,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    registration = db.query(Registration).filter(Registration.registration_id == reg_id).first()
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == registration.email).first()
    if not doc:
        return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)

    if doc.is_approved:
        doc.is_approved = False
        doc.approved_by = None
        doc.approved_at = None
    else:
        doc.is_approved = True
        doc.approved_by = session["email"]
        doc.approved_at = datetime.now(timezone.utc)

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
    price: str = Form(...),
    description: str = Form(""),
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    booth_type = db.query(BoothType).filter(BoothType.id == booth_type_id).first()
    if booth_type and total_quantity >= 0:
        booth_type.total_quantity = total_quantity
        booth_type.description = description.strip()
        try:
            price_cents = int(float(price) * 100)
            if price_cents >= 0:
                booth_type.price = price_cents
        except (ValueError, TypeError):
            pass
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
    event_name: str = Form(...),
    event_start_date: str = Form(...),
    event_end_date: str = Form(...),
    registration_open_date: str = Form(...),
    registration_close_date: str = Form(...),
    banner_text: str = Form(""),
    contact_email: str = Form(""),
    front_page_content: str = Form(""),
    payment_instructions: str = Form(""),
    insurance_instructions: str = Form(""),
    vendor_agreement_text: str = Form(""),
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    settings = db.query(EventSettings).first()
    if settings:
        try:
            settings.event_name = event_name.strip()
            settings.event_start_date = date.fromisoformat(event_start_date)
            settings.event_end_date = date.fromisoformat(event_end_date)
            settings.registration_open_date = datetime.fromisoformat(registration_open_date)
            settings.registration_close_date = datetime.fromisoformat(registration_close_date)
            settings.banner_text = banner_text.strip()
            settings.contact_email = contact_email.strip()
            settings.front_page_content = front_page_content.strip()
            settings.payment_instructions = payment_instructions.strip()
            settings.insurance_instructions = insurance_instructions.strip()
            settings.vendor_agreement_text = vendor_agreement_text.strip()
            db.commit()
            request.app.state.event_name = settings.event_name
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
    insurance_docs = {doc.email: doc for doc in db.query(InsuranceDocument).all()}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Registration ID", "Status", "Business Name", "Contact Name",
        "Email", "Phone", "Category", "Description",
        "Booth Type", "Electrical Equipment", "Electrical Other",
        "Insurance", "Amount Paid", "Refund Amount",
        "Stripe Payment Intent ID", "Created At", "Approved At",
        "Rejected At", "Rejection Reason", "Admin Notes",
    ])

    for reg in registrations:
        ins_doc = insurance_docs.get(reg.email)
        if ins_doc and ins_doc.is_approved:
            insurance_status = "Approved"
        elif ins_doc:
            insurance_status = "Uploaded"
        else:
            insurance_status = "No"

        writer.writerow([
            reg.registration_id,
            reg.status,
            reg.business_name,
            reg.contact_name,
            reg.email,
            reg.phone,
            CATEGORIES.get(reg.category, reg.category),
            reg.description,
            booth_types.get(reg.booth_type_id, "Unknown"),
            reg.electrical_equipment or "",
            reg.electrical_other or "",
            insurance_status,
            f"${reg.amount_paid / 100:.2f}" if reg.amount_paid else "",
            f"${reg.refund_amount / 100:.2f}" if reg.refund_amount else "",
            reg.stripe_payment_intent_id or "",
            reg.created_at.strftime("%Y-%m-%d %H:%M") if reg.created_at else "",
            reg.approved_at.strftime("%Y-%m-%d %H:%M") if reg.approved_at else "",
            reg.rejected_at.strftime("%Y-%m-%d %H:%M") if reg.rejected_at else "",
            reg.rejection_reason or "",
            reg.admin_notes or "",
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
