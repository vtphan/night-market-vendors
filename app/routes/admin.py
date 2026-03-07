import csv
import io
import logging
import zipfile
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Form, Query, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, FileResponse
from sqlalchemy import func as sa_func, extract
from sqlalchemy.orm import Session

from app.database import get_db, get_event_settings, invalidate_event_settings_cache
from app.csrf import generate_csrf_token, require_csrf
from app.session import require_admin
from app.models import Registration, BoothType, InsuranceDocument, AdminNote
from app.models import AdminActivityLog
from app.services.registration import (
    transition_status,
    approve_with_inventory_check,
    get_inventory,
    get_booth_availability,
    get_unpaid_registrations,
    try_cancel_active_payment_intent,
    log_admin_action,
    LOW_INVENTORY_THRESHOLD,
    CATEGORIES,
)
from app.services.email import (
    send_approval_email, send_approval_revoked_email, send_rejection_email,
    send_refund_email, send_admin_alert_email, send_payment_reminder_email,
    send_insurance_reminder_email,
)
from app.services.payment import create_refund
from app.services.food_permit import generate_food_permit, FOOD_CATEGORIES, PERMITS_DIR
from app.config import APP_URL, ADMIN_EMAILS
from app.upload_constants import ALLOWED_EXTENSIONS, ALLOWED_CONTENT_TYPES, MAX_FILE_SIZE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _parse_price_cents(price_str: str) -> int | None:
    """Parse a price string (dollars) into cents. Returns None on invalid input."""
    try:
        price_dec = Decimal(str(price_str))
        if not price_dec.is_finite() or price_dec < 0:
            return None
        return int((price_dec * 100).to_integral_value())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _template(request, name, ctx, session=None):
    """Render a template with standard context."""
    ctx.setdefault("request", request)
    ctx.setdefault("session", session)
    ctx.setdefault("csrf_token", generate_csrf_token())
    ctx.setdefault("get_flashed_messages", lambda: [])
    return request.app.state.templates.TemplateResponse(name, ctx)


def _compute_chart_data(db):
    """Return daily (last 30 days) and hourly registration counts for charts."""
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
    daily_counts = []
    if daily_counts_raw:
        day_map = {str(row[0]): row[1] for row in daily_counts_raw}
        start_date = thirty_days_ago.date()
        end_date = datetime.now(timezone.utc).date()
        current = start_date
        while current <= end_date:
            daily_counts.append({
                "date": current.strftime("%b %d"),
                "count": day_map.get(str(current), 0),
            })
            current += timedelta(days=1)

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

    return daily_counts, hourly_counts


def _inventory_context(db):
    """Build full context for inventory.html renders."""
    inventory = get_inventory(db)
    revenue_by_booth = dict(
        db.query(Registration.booth_type_id, sa_func.sum(Registration.amount_paid))
        .filter(Registration.status == "paid")
        .group_by(Registration.booth_type_id)
        .all()
    )
    refund_by_booth = dict(
        db.query(Registration.booth_type_id, sa_func.sum(Registration.refund_amount))
        .filter(Registration.status.in_(["paid", "cancelled"]))
        .group_by(Registration.booth_type_id)
        .all()
    )
    for item in inventory:
        item["revenue"] = revenue_by_booth.get(item["id"], 0) or 0
        item["refund"] = refund_by_booth.get(item["id"], 0) or 0
    return {
        "inventory": inventory,
        "revenue_total": sum(item["revenue"] for item in inventory),
        "refund_total": sum(item["refund"] for item in inventory),
        "total_capacity": sum(item["total_quantity"] for item in inventory),
        "total_paid": sum(item["paid"] for item in inventory),
        "total_approved": sum(item["approved"] for item in inventory),
        "total_pending": sum(item["pending"] for item in inventory),
    }


def _detail_context(db, registration):
    """Build full context for registration_detail.html re-renders."""
    booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()
    permit_path = PERMITS_DIR / f"{registration.registration_id}.pdf"
    return {
        "registration": registration,
        "booth_type": booth_type,
        "booth_availability": get_booth_availability(db, registration.booth_type_id),
        "LOW_INVENTORY_THRESHOLD": LOW_INVENTORY_THRESHOLD,
        "insurance_doc": db.query(InsuranceDocument).filter(InsuranceDocument.email == registration.email).first(),
        "settings": get_event_settings(db),
        "now": datetime.now(timezone.utc),
        "food_permit_available": permit_path.exists(),
    }


# --- Dashboard ---

@router.get("", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    inventory = get_inventory(db)

    # Status counts for pipeline bar
    status_rows = (
        db.query(Registration.status, sa_func.count(Registration.id))
        .group_by(Registration.status)
        .all()
    )
    status_counts = {row[0]: row[1] for row in status_rows}
    total_registrations = sum(status_counts.values())
    total_vendors = db.query(
        sa_func.count(sa_func.distinct(Registration.email))
    ).scalar() or 0

    # Pending count
    pending_count = status_counts.get("pending", 0)

    # Unpaid registrations + urgency counts
    settings = get_event_settings(db)
    unpaid_registrations = get_unpaid_registrations(db, settings) if settings else []
    unpaid_count = len(unpaid_registrations)
    urgency_counts = {"normal": 0, "reminder_1": 0, "reminder_2": 0, "overdue": 0}
    for item in unpaid_registrations:
        urgency_counts[item["urgency"]] += 1

    # Insurance coverage: vendor-level breakdown
    paid_count = status_counts.get("paid", 0)
    approved_ins_subq = db.query(InsuranceDocument.email).filter(
        InsuranceDocument.is_approved == True
    ).scalar_subquery()
    pending_ins_subq = db.query(InsuranceDocument.email).filter(
        InsuranceDocument.is_approved == False
    ).scalar_subquery()

    active_vendor_count = db.query(
        sa_func.count(sa_func.distinct(Registration.email))
    ).filter(Registration.status.in_(["approved", "paid"])).scalar() or 0
    vendors_with_approved_doc = db.query(
        sa_func.count(sa_func.distinct(Registration.email))
    ).filter(
        Registration.status.in_(["approved", "paid"]),
        Registration.email.in_(approved_ins_subq),
    ).scalar() or 0
    vendors_with_pending_doc = db.query(
        sa_func.count(sa_func.distinct(Registration.email))
    ).filter(
        Registration.status.in_(["approved", "paid"]),
        Registration.email.in_(pending_ins_subq),
    ).scalar() or 0
    vendors_without_doc = active_vendor_count - vendors_with_approved_doc - vendors_with_pending_doc

    active_without_approved_ins = active_vendor_count - vendors_with_approved_doc

    # Food permit counts (only approved/paid — permits are auto-generated on approval)
    food_bev_regs = db.query(Registration.registration_id).filter(
        Registration.status.in_(["approved", "paid"]),
        Registration.category.in_(list(FOOD_CATEGORIES)),
    ).all()
    food_bev_total = len(food_bev_regs)
    permits_generated = sum(
        1 for (rid,) in food_bev_regs
        if (PERMITS_DIR / f"{rid}.pdf").exists()
    )
    permits_missing = food_bev_total - permits_generated

    # Meta line
    last_registration = (
        db.query(Registration.created_at)
        .order_by(Registration.created_at.desc())
        .first()
    )
    last_registration_at = last_registration[0] if last_registration else None

    return _template(request, "admin/dashboard.html", {
        "inventory": inventory,
        "pending_count": pending_count,
        "unpaid_count": unpaid_count,
        "urgency_counts": urgency_counts,
        "active_vendor_count": active_vendor_count,
        "vendors_without_doc": vendors_without_doc,
        "vendors_with_pending_doc": vendors_with_pending_doc,
        "vendors_with_approved_doc": vendors_with_approved_doc,
        "active_without_approved_ins": active_without_approved_ins,
        "food_bev_total": food_bev_total,
        "permits_generated": permits_generated,
        "permits_missing": permits_missing,
        "paid_count": paid_count,
        "last_registration_at": last_registration_at,
        "status_counts": status_counts,
        "total_registrations": total_registrations,
        "total_vendors": total_vendors,
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
    permit: str = Query("", alias="permit"),
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
    if permit == "missing":
        query = query.filter(
            Registration.category.in_(list(FOOD_CATEGORIES)),
            Registration.status.in_(["approved", "paid"]),
        )
    elif permit == "generated":
        query = query.filter(
            Registration.category.in_(list(FOOD_CATEGORIES)),
            Registration.status.in_(["approved", "paid"]),
        )
    elif permit == "na":
        query = query.filter(~Registration.category.in_(list(FOOD_CATEGORIES)))
    if notes == "yes":
        has_notes = db.query(AdminNote.registration_id).distinct().subquery()
        query = query.filter(Registration.registration_id.in_(has_notes))
    elif notes == "flagged":
        query = query.filter(Registration.concern_status == "yes")
    if search:
        # Escape SQL LIKE wildcards in user input to prevent unintended matching
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        term = f"%{escaped}%"
        query = query.filter(
            (Registration.business_name.ilike(term, escape="\\"))
            | (Registration.contact_name.ilike(term, escape="\\"))
            | (Registration.email.ilike(term, escape="\\"))
            | (Registration.registration_id.ilike(term, escape="\\"))
        )

    registrations = query.order_by(Registration.created_at.desc()).all()

    # Post-query filter for permit status (file-based check)
    if permit in ("missing", "generated"):
        filtered = []
        for r in registrations:
            has_permit = (PERMITS_DIR / f"{r.registration_id}.pdf").exists()
            if permit == "missing" and not has_permit:
                filtered.append(r)
            elif permit == "generated" and has_permit:
                filtered.append(r)
        registrations = filtered

    booth_types = {bt.id: bt for bt in db.query(BoothType).all()}

    # Build insurance doc lookup by email (scoped to displayed registrations)
    relevant_emails = {r.email for r in registrations}
    insurance_docs = {
        doc.email: doc
        for doc in db.query(InsuranceDocument).filter(InsuranceDocument.email.in_(relevant_emails)).all()
    } if relevant_emails else {}

    # Build permit status lookup (only for food/bev registrations)
    permit_status = {}
    for r in registrations:
        if r.category in FOOD_CATEGORIES:
            permit_status[r.registration_id] = (PERMITS_DIR / f"{r.registration_id}.pdf").exists()
        # Non-food/bev registrations are omitted (N/A)

    daily_counts, hourly_counts = _compute_chart_data(db)

    regs_with_notes = set(
        r[0] for r in db.query(AdminNote.registration_id).distinct().all()
    )

    settings = get_event_settings(db)

    return _template(request, "admin/registrations.html", {
        "registrations": registrations,
        "booth_types": booth_types,
        "insurance_docs": insurance_docs,
        "permit_status": permit_status,
        "regs_with_notes": regs_with_notes,
        "filter_status": status,
        "filter_category": category,
        "filter_booth_type": booth_type,
        "filter_insurance": insurance,
        "filter_permit": permit,
        "filter_search": search,
        "daily_counts": daily_counts,
        "hourly_counts": hourly_counts,
        "now": datetime.utcnow(),
        "reminder_1_days": settings.reminder_1_days if settings else 2,
        "reminder_2_days": settings.reminder_2_days if settings else 5,
        "payment_deadline_days": settings.payment_deadline_days if settings else 7,
    }, session=session)


# --- Notes page ---

@router.get("/notes", response_class=HTMLResponse)
async def notes_page(
    request: Request,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    sort: str = Query("date", alias="sort"),
    order: str = Query("desc", alias="order"),
):
    # Registrations that have at least one note
    reg_ids_with_notes = db.query(AdminNote.registration_id).distinct().subquery()
    registrations = (
        db.query(Registration)
        .filter(Registration.registration_id.in_(reg_ids_with_notes))
        .all()
    )

    # Build latest note lookup in a single query
    latest_subq = (
        db.query(
            AdminNote.registration_id,
            sa_func.max(AdminNote.created_at).label("max_date"),
        )
        .group_by(AdminNote.registration_id)
        .subquery()
    )
    latest_note_rows = (
        db.query(AdminNote)
        .join(latest_subq, (AdminNote.registration_id == latest_subq.c.registration_id)
              & (AdminNote.created_at == latest_subq.c.max_date))
        .all()
    )
    latest_notes = {n.registration_id: n.text for n in latest_note_rows}
    latest_note_dates = {n.registration_id: n.created_at for n in latest_note_rows}

    # Sort
    reverse = order == "desc"
    if sort == "id":
        registrations.sort(key=lambda r: r.registration_id, reverse=reverse)
    elif sort == "flag":
        registrations.sort(key=lambda r: (r.concern_status == "yes", r.registration_id), reverse=reverse)
    else:  # date (default)
        registrations.sort(
            key=lambda r: latest_note_dates.get(r.registration_id, datetime.min),
            reverse=reverse,
        )

    booth_types = {bt.id: bt for bt in db.query(BoothType).all()}
    return _template(request, "admin/notes.html", {
        "registrations": registrations,
        "latest_notes": latest_notes,
        "latest_note_dates": latest_note_dates,
        "booth_types": booth_types,
        "current_sort": sort,
        "current_order": order,
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
    settings = get_event_settings(db)
    notes = (
        db.query(AdminNote)
        .filter(AdminNote.registration_id == reg_id)
        .order_by(AdminNote.created_at.desc())
        .all()
    )

    permit_path = PERMITS_DIR / f"{reg_id}.pdf"

    return _template(request, "admin/registration_detail.html", {
        "registration": registration,
        "booth_type": booth_type,
        "booth_availability": available,
        "LOW_INVENTORY_THRESHOLD": LOW_INVENTORY_THRESHOLD,
        "insurance_doc": insurance_doc,
        "settings": settings,
        "now": datetime.now(timezone.utc),
        "notes": notes,
        "food_permit_available": permit_path.exists(),
    }, session=session)


# --- Activity log ---

ACTION_LABELS = {
    "approved": "Approved",
    "rejected": "Rejected",
    "revoked_approval": "Revoked Approval",
    "unrejected": "Unrejected",
    "cancelled": "Cancelled & Refunded",
    "approved_insurance": "Approved Insurance",
    "revoked_insurance": "Revoked Insurance",
    "sent_payment_reminder": "Sent Payment Reminder",
    "sent_insurance_reminder": "Sent Insurance Reminder",
}


@router.get("/logs")
async def activity_logs(
    request: Request,
    page: int = Query(1, ge=1),
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    per_page = 50
    total = db.query(sa_func.count(AdminActivityLog.id)).scalar()
    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)

    logs = (
        db.query(AdminActivityLog)
        .order_by(AdminActivityLog.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return _template(request, "admin/logs.html", {
        "logs": logs,
        "action_labels": ACTION_LABELS,
        "page": page,
        "total_pages": total_pages,
        "total": total,
    }, session=session)


# --- Approve registration ---

@router.post("/registrations/{reg_id}/approve")
async def approve_registration(
    request: Request,
    reg_id: str,
    background_tasks: BackgroundTasks,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == reg_id)
        .with_for_update()
        .first()
    )
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    try:
        approve_with_inventory_check(db, registration)
    except ValueError as e:
        logger.warning("Cannot approve %s: %s", reg_id, e)
        flash = [{"category": "error", "text": f"Cannot approve: {e}"}]
        ctx = _detail_context(db, registration)
        ctx["get_flashed_messages"] = lambda: flash
        return _template(request, "admin/registration_detail.html", ctx, session=session)

    # Extract just the domain so the email tells vendors where to log in
    # without embedding a direct link (reduces spam-filter risk).
    portal_domain = urlparse(APP_URL).hostname or APP_URL
    settings = get_event_settings(db)
    deadline_date = (
        registration.payment_deadline.strftime("%b %d, %Y")
        if registration.payment_deadline else None
    )
    background_tasks.add_task(
        send_approval_email,
        registration.email, reg_id, portal_domain,
        insurance_instructions=settings.insurance_instructions if settings else "",
        deadline_date=deadline_date,
    )

    # Auto-generate food permit for food/beverage vendors
    if registration.category in FOOD_CATEGORIES:
        event_name = settings.event_name if settings else "Asian Night Market"
        event_location = "Agricenter Outdoor, 7777 Walnut Grove Rd, Memphis, TN"
        event_dates = ""
        if settings:
            start = settings.event_start_date.strftime("%b %d")
            end = settings.event_end_date.strftime("%b %d, %Y")
            event_dates = f"{start}-{end}"
        generate_food_permit(
            registration_id=reg_id,
            category=registration.category,
            business_name=registration.business_name,
            contact_name=registration.contact_name,
            address=registration.address,
            city_state_zip=registration.city_state_zip,
            phone=registration.phone,
            email=registration.email,
            description=registration.description,
            event_name=event_name,
            event_location=event_location,
            event_dates=event_dates,
        )

    log_admin_action(db, session["email"], "approved", reg_id, registration.business_name)

    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Send payment reminder ---

def _reminder_template_vars(registration, db):
    """Build the subject, body, and format variables for a payment reminder."""
    from app.services.email import _get_email_globals

    settings = get_event_settings(db)
    if (registration.reminder_count or 0) == 0 and settings:
        subject_tpl = settings.reminder_1_subject or "Payment Reminder — {event_name}"
        body_tpl = settings.reminder_1_body or ""
    elif settings:
        subject_tpl = settings.reminder_2_subject or "Urgent: Payment Deadline Approaching — {event_name}"
        body_tpl = settings.reminder_2_body or ""
    else:
        subject_tpl = "Payment Reminder — {event_name}"
        body_tpl = ""

    portal_domain = urlparse(APP_URL).hostname or APP_URL
    deadline_date = (
        registration.payment_deadline.strftime("%b %d, %Y")
        if registration.payment_deadline else "N/A"
    )
    globals = _get_email_globals()
    fmt_vars = {
        "registration_id": registration.registration_id,
        "portal_domain": portal_domain,
        "deadline_date": deadline_date,
        "event_name": globals.get("event_name", ""),
        "contact_email": globals.get("contact_email", ""),
    }
    # Normalize literal \n sequences to actual newlines (plain text templates)
    body_tpl = body_tpl.replace('\\n', '\n')

    try:
        subject = subject_tpl.format(**fmt_vars)
        body = body_tpl.format(**fmt_vars)
    except (KeyError, IndexError):
        subject = subject_tpl
        body = body_tpl

    return subject, body


@router.get("/registrations/{reg_id}/remind/preview")
async def remind_preview(
    reg_id: str,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == reg_id)
        .first()
    )
    if not registration or registration.status != "approved":
        return JSONResponse({"error": "Not found or not approved"}, status_code=400)

    subject, body = _reminder_template_vars(registration, db)
    return JSONResponse({"subject": subject, "body": body, "to": registration.email})


@router.post("/registrations/{reg_id}/remind")
async def send_reminder(
    request: Request,
    reg_id: str,
    background_tasks: BackgroundTasks,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
    custom_subject: str = Form(""),
    custom_body: str = Form(""),
):
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == reg_id)
        .first()
    )
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    if registration.status != "approved":
        flash = [{"category": "error", "text": "Can only send reminders for approved registrations."}]
        ctx = _detail_context(db, registration)
        ctx["get_flashed_messages"] = lambda: flash
        return _template(request, "admin/registration_detail.html", ctx, session=session)

    # Rate limit: 1 reminder per hour
    now = datetime.now(timezone.utc)
    if registration.last_reminder_sent_at:
        last = registration.last_reminder_sent_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < 3600:
            flash = [{"category": "error", "text": "A reminder was sent less than 1 hour ago. Please wait before sending another."}]
            ctx = _detail_context(db, registration)
            ctx["get_flashed_messages"] = lambda: flash
            return _template(request, "admin/registration_detail.html", ctx, session=session)

    # Use admin-edited content if provided, otherwise fall back to templates
    if custom_subject.strip() and custom_body.strip():
        subject_tpl = custom_subject.strip()
        body_tpl = custom_body.strip()
    else:
        subject_tpl, body_tpl = _reminder_template_vars(registration, db)

    portal_domain = urlparse(APP_URL).hostname or APP_URL
    deadline_date = (
        registration.payment_deadline.strftime("%b %d, %Y")
        if registration.payment_deadline else "N/A"
    )

    background_tasks.add_task(
        send_payment_reminder_email,
        registration.email,
        reg_id,
        portal_domain,
        deadline_date,
        subject_tpl,
        body_tpl,
    )

    registration.last_reminder_sent_at = now
    registration.reminder_count = (registration.reminder_count or 0) + 1
    db.commit()

    log_admin_action(db, session["email"], "sent_payment_reminder", reg_id, f"Reminder #{registration.reminder_count}")

    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Reject registration ---

@router.post("/registrations/{reg_id}/reject")
async def reject_registration(
    request: Request,
    reg_id: str,
    background_tasks: BackgroundTasks,
    reversal_reason: str = Form(""),
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == reg_id)
        .with_for_update()
        .first()
    )
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    reversal_reason = reversal_reason.strip()
    if not reversal_reason:
        flash = [{"category": "error", "text": "A rejection reason is required."}]
        ctx = _detail_context(db, registration)
        ctx["get_flashed_messages"] = lambda: flash
        return _template(request, "admin/registration_detail.html", ctx, session=session)

    # Cancel active PI before revoking approval
    if registration.status == "approved" and registration.stripe_payment_intent_id:
        ok, msg = try_cancel_active_payment_intent(registration)
        if not ok:
            flash = [{"category": "error", "text": msg}]
            ctx = _detail_context(db, registration)
            ctx["get_flashed_messages"] = lambda: flash
            return _template(request, "admin/registration_detail.html", ctx, session=session)

    try:
        transition_status(db, registration, "rejected", reversal_reason=reversal_reason)
    except ValueError as e:
        logger.warning("Invalid transition for %s: %s", reg_id, e)
        flash = [{"category": "error", "text": f"Cannot reject: {e}"}]
        ctx = _detail_context(db, registration)
        ctx["get_flashed_messages"] = lambda: flash
        return _template(request, "admin/registration_detail.html", ctx, session=session)

    # Remove food permit PDF if it exists (no longer approved)
    permit_path = PERMITS_DIR / f"{reg_id}.pdf"
    if permit_path.exists():
        permit_path.unlink()

    background_tasks.add_task(send_rejection_email, registration.email, reg_id, reversal_reason or None)

    log_admin_action(db, session["email"], "rejected", reg_id, reversal_reason or None)

    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Unreject registration (back to pending) ---

@router.post("/registrations/{reg_id}/unreject")
async def unreject_registration(
    request: Request,
    reg_id: str,
    background_tasks: BackgroundTasks,
    reversal_reason: str = Form(""),
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == reg_id)
        .with_for_update()
        .first()
    )
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    reversal_reason = reversal_reason.strip()
    if not reversal_reason:
        flash = [{"category": "error", "text": "A reason is required."}]
        ctx = _detail_context(db, registration)
        ctx["get_flashed_messages"] = lambda: flash
        return _template(request, "admin/registration_detail.html", ctx, session=session)

    was_approved = registration.status == "approved"

    # Cancel active PI before revoking approval
    if was_approved and registration.stripe_payment_intent_id:
        ok, msg = try_cancel_active_payment_intent(registration)
        if not ok:
            flash = [{"category": "error", "text": msg}]
            ctx = _detail_context(db, registration)
            ctx["get_flashed_messages"] = lambda: flash
            return _template(request, "admin/registration_detail.html", ctx, session=session)

    try:
        transition_status(db, registration, "pending", reversal_reason=reversal_reason)
    except ValueError as e:
        logger.warning("Invalid transition for %s: %s", reg_id, e)
        flash = [{"category": "error", "text": f"Cannot unreject: {e}"}]
        ctx = _detail_context(db, registration)
        ctx["get_flashed_messages"] = lambda: flash
        return _template(request, "admin/registration_detail.html", ctx, session=session)

    # Remove food permit PDF if it exists (no longer approved)
    permit_path = PERMITS_DIR / f"{reg_id}.pdf"
    if permit_path.exists():
        permit_path.unlink()

    # Notify the vendor when a previously approved registration is revoked
    if was_approved:
        background_tasks.add_task(
            send_approval_revoked_email,
            registration.email, reg_id, reversal_reason or None,
        )

    action = "revoked_approval" if was_approved else "unrejected"
    log_admin_action(db, session["email"], action, reg_id, reversal_reason or None)

    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Cancel + Refund ---

@router.post("/registrations/{reg_id}/cancel")
async def cancel_registration(
    request: Request,
    reg_id: str,
    background_tasks: BackgroundTasks,
    refund_amount: str = Form("0"),
    reversal_reason: str = Form(""),
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    # Lock the registration row to prevent concurrent cancel/refund
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == reg_id)
        .with_for_update()
        .first()
    )
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    if registration.status != "paid":
        logger.warning("Cannot cancel %s: status is %s", reg_id, registration.status)
        return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)

    reversal_reason = reversal_reason.strip()
    if not reversal_reason:
        flash = [{"category": "error", "text": "A reason is required."}]
        ctx = _detail_context(db, registration)
        ctx["get_flashed_messages"] = lambda: flash
        return _template(request, "admin/registration_detail.html", ctx, session=session)

    # Convert dollar amount to cents using Decimal to avoid float precision errors
    try:
        amount_dec = Decimal(refund_amount)
        if amount_dec < 0 or not amount_dec.is_finite():
            amount_cents = 0
        else:
            amount_cents = int((amount_dec * 100).to_integral_value())
    except (InvalidOperation, ValueError, TypeError):
        amount_cents = 0

    # Validate refund does not exceed amount paid minus prior refunds
    max_refundable = (registration.amount_paid or 0) - (registration.refund_amount or 0)
    if amount_cents > max_refundable:
        flash = [{"category": "error", "text": f"Refund amount (${amount_cents / 100:.2f}) exceeds amount paid (${max_refundable / 100:.2f})."}]
        ctx = _detail_context(db, registration)
        ctx["get_flashed_messages"] = lambda: flash
        return _template(request, "admin/registration_detail.html", ctx, session=session)

    if amount_cents > 0 and not registration.stripe_payment_intent_id:
        flash = [{"category": "error", "text": "Cannot refund: no payment record found."}]
        ctx = _detail_context(db, registration)
        ctx["get_flashed_messages"] = lambda: flash
        return _template(request, "admin/registration_detail.html", ctx, session=session)

    # Step 1: Commit cancellation to DB first — no money moves yet.
    try:
        transition_status(db, registration, "cancelled", reversal_reason=reversal_reason)
    except ValueError as e:
        logger.warning("Invalid transition for %s: %s", reg_id, e)
        db.rollback()
        return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)

    # Step 2: Issue Stripe refund (only after DB is consistent).
    if amount_cents > 0 and registration.stripe_payment_intent_id:
        try:
            create_refund(db, registration, amount_cents)
        except Exception:
            logger.exception("Stripe refund failed for %s after cancellation", reg_id)
            background_tasks.add_task(
                send_admin_alert_email,
                f"Action required: refund failed — {reg_id}",
                f"Registration {reg_id} ({registration.business_name}) was cancelled "
                f"but the Stripe refund of ${amount_cents / 100:.2f} failed.\n\n"
                f"Action required:\n"
                f"1. Issue the refund manually in the Stripe Dashboard.\n"
                f"2. Notify the vendor ({registration.email}) about the cancellation "
                f"and refund status.\n\n"
                f"The vendor has NOT been automatically notified.\n"
                f"PaymentIntent: {registration.stripe_payment_intent_id}",
            )
            flash = [{"category": "error", "text": "Registration cancelled but refund failed. Please issue the refund via Stripe Dashboard."}]
            ctx = _detail_context(db, registration)
            ctx["get_flashed_messages"] = lambda: flash
            return _template(request, "admin/registration_detail.html", ctx, session=session)

        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("DB commit failed after successful Stripe refund for %s", reg_id)
            background_tasks.add_task(
                send_admin_alert_email,
                f"Refund succeeded but failed to record — {reg_id}",
                f"Registration {reg_id} ({registration.business_name}): the Stripe refund "
                f"of ${amount_cents / 100:.2f} SUCCEEDED, but failed to save to the database.\n\n"
                f"DO NOT issue another refund. The charge.refunded webhook will sync "
                f"the amount automatically.\n\n"
                f"Please notify the vendor ({registration.email}) about the cancellation "
                f"and refund. The vendor has NOT been automatically notified.\n"
                f"PaymentIntent: {registration.stripe_payment_intent_id}",
            )
            flash = [{"category": "error", "text": "Refund was issued but failed to record. Do NOT re-issue — it will sync automatically."}]
            ctx = _detail_context(db, registration)
            ctx["get_flashed_messages"] = lambda: flash
            return _template(request, "admin/registration_detail.html", ctx, session=session)

    # Notify vendor
    background_tasks.add_task(
        send_refund_email, registration.email, reg_id, amount_cents,
        reason=reversal_reason or None,
        processing_fee_cents=registration.processing_fee or 0,
    )

    # Notify all admins for financial records
    background_tasks.add_task(
        send_admin_alert_email,
        f"Registration cancelled & refunded — {reg_id}",
        f"Registration {reg_id} ({registration.business_name}) has been cancelled "
        f"by {session.get('email', 'unknown')}.\n\n"
        f"Reason: {reversal_reason}\n"
        f"Refund amount: ${amount_cents / 100:.2f}\n"
        f"PaymentIntent: {registration.stripe_payment_intent_id or 'N/A'}",
    )

    refund_note = f"Refund: ${amount_cents / 100:.2f}" if amount_cents > 0 else "No refund"
    log_admin_action(db, session["email"], "cancelled", reg_id, f"{reversal_reason} ({refund_note})")

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
        .with_for_update()
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
async def add_note(
    request: Request,
    reg_id: str,
    note_text: str = Form(""),
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

    text = note_text.strip()[:500]
    if text:
        note = AdminNote(
            registration_id=reg_id,
            admin_email=session["email"],
            text=text,
        )
        db.add(note)
        db.commit()
    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


@router.post("/registrations/{reg_id}/flag")
async def toggle_flag(
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

    registration.concern_status = "none" if registration.concern_status == "yes" else "yes"
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
    if ".." in stored_filename or "/" in stored_filename or "\\" in stored_filename:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    doc = db.query(InsuranceDocument).filter(InsuranceDocument.stored_filename == stored_filename).first()
    if not doc:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    uploads_dir = request.app.state.uploads_dir
    file_path = (uploads_dir / stored_filename).resolve()
    if not file_path.is_relative_to(uploads_dir.resolve()):
        return RedirectResponse(url="/admin/registrations", status_code=303)
    if not file_path.exists():
        return RedirectResponse(url="/admin/registrations", status_code=303)

    return FileResponse(
        path=str(file_path),
        media_type=doc.content_type,
        filename=doc.original_filename,
    )


# --- Food permit download ---

@router.get("/registrations/{reg_id}/food-permit")
async def download_food_permit(
    reg_id: str,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    registration = db.query(Registration).filter(Registration.registration_id == reg_id).first()
    if not registration or registration.status not in ("approved", "paid"):
        return RedirectResponse(url=f"/admin/registrations/{reg_id}" if registration else "/admin/registrations", status_code=303)

    permit_path = PERMITS_DIR / f"{reg_id}.pdf"
    if not permit_path.exists():
        return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)

    return FileResponse(
        path=str(permit_path),
        media_type="application/pdf",
        filename=f"food_permit_{reg_id}.pdf",
    )


@router.post("/registrations/{reg_id}/food-permit/generate")
async def generate_food_permit_route(
    reg_id: str,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    registration = db.query(Registration).filter(Registration.registration_id == reg_id).first()
    if not registration or registration.category not in FOOD_CATEGORIES or registration.status not in ("approved", "paid"):
        return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)

    settings = get_event_settings(db)
    event_location = "Agricenter Outdoor, 7777 Walnut Grove Rd, Memphis, TN"
    event_dates = ""
    if settings:
        start = settings.event_start_date.strftime("%b %d")
        end = settings.event_end_date.strftime("%b %d, %Y")
        event_dates = f"{start}-{end}"

    generate_food_permit(
        registration_id=reg_id,
        category=registration.category,
        business_name=registration.business_name,
        contact_name=registration.contact_name,
        address=registration.address,
        city_state_zip=registration.city_state_zip,
        phone=registration.phone,
        email=registration.email,
        description=registration.description,
        event_name=settings.event_name if settings else "Asian Night Market",
        event_location=event_location,
        event_dates=event_dates,
    )

    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Download insurance / permits ---

@router.get("/download-insurance")
async def download_all_insurance(
    request: Request,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    uploads_dir: Path = request.app.state.uploads_dir
    docs = db.query(InsuranceDocument).all()

    # Map email -> latest active registration ID for filenames
    registrations = db.query(Registration).filter(
        Registration.status.in_(["pending", "approved", "paid"]),
    ).all()
    email_to_reg = {}
    for reg in registrations:
        email_to_reg.setdefault(reg.email, reg.registration_id)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in docs:
            file_path = uploads_dir / doc.stored_filename
            if not file_path.exists():
                continue
            reg_id = email_to_reg.get(doc.email, doc.email)
            ext = Path(doc.original_filename).suffix
            arcname = f"insurance/{reg_id}_{doc.email}{ext}"
            zf.write(file_path, arcname)

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=insurance_documents.zip"},
    )


@router.get("/download-permits")
async def download_all_permits(
    session: dict = Depends(require_admin),
):
    if not PERMITS_DIR.exists():
        return RedirectResponse(url="/admin/registrations", status_code=303)

    permit_files = list(PERMITS_DIR.glob("*.pdf"))
    if not permit_files:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in permit_files:
            zf.write(fp, f"permits/{fp.name}")

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=food_permits.zip"},
    )


@router.post("/registrations/{reg_id}/insurance/approve")
async def approve_insurance(
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

    if not doc.is_approved:
        doc.is_approved = True
        doc.approved_by = session["email"]
        doc.approved_at = datetime.now(timezone.utc)
        db.commit()
        log_admin_action(db, session["email"], "approved_insurance", reg_id, registration.email)

    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


@router.post("/registrations/{reg_id}/insurance/revoke")
async def revoke_insurance(
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
        db.commit()
        log_admin_action(db, session["email"], "revoked_insurance", reg_id, registration.email)

    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


@router.post("/registrations/{reg_id}/insurance/upload")
async def admin_insurance_upload(
    request: Request,
    reg_id: str,
    file: UploadFile = File(...),
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    registration = db.query(Registration).filter(Registration.registration_id == reg_id).first()
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    email = registration.email
    uploads_dir: Path = request.app.state.uploads_dir

    def _error_response(msg):
        flash = [{"category": "error", "text": msg}]
        ctx = _detail_context(db, registration)
        ctx["get_flashed_messages"] = lambda: flash
        return _template(request, "admin/registration_detail.html", ctx, session=session)

    # Validate extension
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return _error_response("File type not allowed. Please upload a PDF, PNG, or JPG file.")

    # Validate content type
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        return _error_response("File type not allowed. Please upload a PDF, PNG, or JPG file.")

    # Read file in chunks
    chunks = []
    total_size = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > MAX_FILE_SIZE:
            break
        chunks.append(chunk)
    contents = b"".join(chunks)

    if total_size > MAX_FILE_SIZE:
        return _error_response("File is too large. Maximum size is 10 MB.")

    stored_filename = f"{uuid4().hex}{ext}"
    file_path = uploads_dir / stored_filename

    # Create or replace existing document
    existing = db.query(InsuranceDocument).filter(InsuranceDocument.email == email).first()
    old_stored_filename = None
    if existing:
        old_stored_filename = existing.stored_filename
        existing.original_filename = file.filename or "unknown"
        existing.stored_filename = stored_filename
        existing.content_type = file.content_type
        existing.file_size = len(contents)
        existing.is_approved = False
        existing.approved_by = None
        existing.approved_at = None
        existing.uploaded_at = datetime.now(timezone.utc)
    else:
        doc = InsuranceDocument(
            email=email,
            original_filename=file.filename or "unknown",
            stored_filename=stored_filename,
            content_type=file.content_type,
            file_size=len(contents),
        )
        db.add(doc)

    with open(file_path, "wb") as f:
        f.write(contents)

    try:
        db.commit()
    except Exception:
        if file_path.exists():
            file_path.unlink()
        raise

    if old_stored_filename:
        old_path = uploads_dir / old_stored_filename
        if old_path.exists():
            old_path.unlink()

    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Insurance reminder ---

def _insurance_reminder_defaults(registration, db):
    """Build default subject and body for an insurance reminder."""
    from app.services.email import _get_email_globals

    portal_domain = urlparse(APP_URL).hostname or APP_URL
    globals = _get_email_globals()
    event_name = globals.get("event_name", "")

    subject = f"Insurance Document Reminder — {event_name}"
    body = (
        f"Hi,\n\n"
        f"This is a reminder that we still need your insurance document "
        f"for registration {registration.registration_id}.\n\n"
        f"Please log in to the vendor portal at {portal_domain} "
        f"to view the insurance requirements and upload your document.\n\n"
        f"Thank you!"
    )
    return subject, body


@router.get("/registrations/{reg_id}/insurance-remind/preview")
async def insurance_remind_preview(
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
        return JSONResponse({"error": "Not found"}, status_code=400)

    # Only allow if no insurance document uploaded yet
    doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == registration.email).first()
    if doc:
        return JSONResponse({"error": "Insurance document already uploaded"}, status_code=400)

    subject, body = _insurance_reminder_defaults(registration, db)
    return JSONResponse({"subject": subject, "body": body, "to": registration.email})


@router.post("/registrations/{reg_id}/insurance-remind")
async def send_insurance_reminder(
    request: Request,
    reg_id: str,
    background_tasks: BackgroundTasks,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
    custom_subject: str = Form(""),
    custom_body: str = Form(""),
):
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == reg_id)
        .first()
    )
    if not registration:
        return RedirectResponse(url="/admin/registrations", status_code=303)

    # Only allow if no insurance document uploaded yet
    doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == registration.email).first()
    if doc:
        flash = [{"category": "error", "text": "Insurance document already uploaded. No reminder needed."}]
        ctx = _detail_context(db, registration)
        ctx["get_flashed_messages"] = lambda: flash
        return _template(request, "admin/registration_detail.html", ctx, session=session)

    # Rate limit: 1 reminder per hour
    now = datetime.now(timezone.utc)
    if registration.last_insurance_reminder_sent_at:
        last = registration.last_insurance_reminder_sent_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < 3600:
            flash = [{"category": "error", "text": "An insurance reminder was sent less than 1 hour ago. Please wait before sending another."}]
            ctx = _detail_context(db, registration)
            ctx["get_flashed_messages"] = lambda: flash
            return _template(request, "admin/registration_detail.html", ctx, session=session)

    if custom_subject.strip() and custom_body.strip():
        subject_text = custom_subject.strip()
        body_text = custom_body.strip()
    else:
        subject_text, body_text = _insurance_reminder_defaults(registration, db)

    portal_domain = urlparse(APP_URL).hostname or APP_URL

    background_tasks.add_task(
        send_insurance_reminder_email,
        registration.email,
        reg_id,
        portal_domain,
        subject_text,
        body_text,
    )

    registration.last_insurance_reminder_sent_at = now
    registration.insurance_reminder_count = (registration.insurance_reminder_count or 0) + 1
    db.commit()

    log_admin_action(db, session["email"], "sent_insurance_reminder", reg_id, f"Reminder #{registration.insurance_reminder_count}")

    return RedirectResponse(url=f"/admin/registrations/{reg_id}", status_code=303)


# --- Inventory ---

@router.get("/inventory", response_class=HTMLResponse)
async def inventory_page(
    request: Request,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return _template(request, "admin/inventory.html",
                     _inventory_context(db), session=session)


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
    # Lock the row to prevent races with concurrent approvals.
    booth_type = db.query(BoothType).filter(BoothType.id == booth_type_id).with_for_update().first()
    if booth_type and total_quantity >= 0:
        # Prevent setting quantity below currently reserved (approved + paid) count
        reserved = (
            db.query(sa_func.count(Registration.id))
            .filter(
                Registration.booth_type_id == booth_type_id,
                Registration.status.in_(["approved", "paid"]),
            )
            .scalar()
        ) or 0
        if total_quantity < reserved:
            flash = [{"category": "error", "text": f"Cannot set quantity below {reserved} (currently reserved)."}]
            ctx = _inventory_context(db)
            ctx["get_flashed_messages"] = lambda: flash
            return _template(request, "admin/inventory.html", ctx, session=session)
        booth_type.total_quantity = total_quantity
        booth_type.description = description.strip()
        parsed = _parse_price_cents(price)
        if parsed is not None:
            booth_type.price = parsed
        db.commit()
    return RedirectResponse(url="/admin/inventory", status_code=303)


@router.post("/inventory")
async def update_inventory_bulk(
    request: Request,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    form = await request.form()
    booth_types = db.query(BoothType).filter(BoothType.is_active == True).with_for_update().all()
    errors = []
    for bt in booth_types:
        prefix = f"bt_{bt.id}_"
        raw_qty = form.get(f"{prefix}total_quantity")
        raw_price = form.get(f"{prefix}price")
        raw_desc = form.get(f"{prefix}description", "")
        if raw_qty is None or raw_price is None:
            continue
        try:
            qty = int(raw_qty)
        except (ValueError, TypeError):
            continue
        if qty < 0:
            continue
        reserved = (
            db.query(sa_func.count(Registration.id))
            .filter(
                Registration.booth_type_id == bt.id,
                Registration.status.in_(["approved", "paid"]),
            )
            .scalar()
        ) or 0
        if qty < reserved:
            errors.append(f"{bt.name}: cannot set quantity below {reserved} (currently reserved).")
            continue
        bt.total_quantity = qty
        bt.description = str(raw_desc).strip()
        parsed = _parse_price_cents(raw_price)
        if parsed is not None:
            bt.price = parsed
    if errors:
        db.rollback()
        flash = [{"category": "error", "text": e} for e in errors]
        ctx = _inventory_context(db)
        ctx["get_flashed_messages"] = lambda: flash
        return _template(request, "admin/inventory.html", ctx, session=session)
    db.commit()
    return RedirectResponse(url="/admin/inventory", status_code=303)


# --- Settings ---

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    settings = get_event_settings(db)
    return _template(request, "admin/settings.html", {
        "settings": settings,
        "admin_emails": ADMIN_EMAILS,
    }, session=session)


@router.post("/settings")
async def update_settings(
    request: Request,
    event_name: str = Form(...),
    event_start_date: str = Form(...),
    event_end_date: str = Form(...),
    registration_open_date: str = Form(...),
    registration_close_date: str = Form(...),
    event_timezone: str = Form("America/Chicago"),
    banner_text: str = Form(""),
    contact_email: str = Form(""),
    developer_contact: str = Form(""),
    front_page_content: str = Form(""),
    payment_instructions: str = Form(""),
    insurance_instructions: str = Form(""),
    vendor_agreement_text: str = Form(""),
    processing_fee_percent: str = Form("0"),
    processing_fee_flat_cents: str = Form("0"),
    refund_policy: str = Form(""),
    refund_presets: str = Form("100,75,50,25,0"),
    notify_new_registration: str | None = Form(None),
    notify_payment_received: str | None = Form(None),
    notify_insurance_uploaded: str | None = Form(None),
    payment_deadline_days: str = Form("7"),
    reminder_1_days: str = Form("2"),
    reminder_2_days: str = Form("5"),
    reminder_1_subject: str = Form(""),
    reminder_1_body: str = Form(""),
    reminder_2_subject: str = Form(""),
    reminder_2_body: str = Form(""),
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    settings = get_event_settings(db)
    if settings:
        try:
            settings.event_name = event_name.strip()
            settings.event_start_date = date.fromisoformat(event_start_date)
            settings.event_end_date = date.fromisoformat(event_end_date)
            tz_name = event_timezone.strip() or "America/Chicago"
            event_tz = ZoneInfo(tz_name)
            open_dt = datetime.fromisoformat(registration_open_date).replace(tzinfo=event_tz)
            close_dt = datetime.fromisoformat(registration_close_date).replace(tzinfo=event_tz)
            settings.registration_open_date = open_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            settings.registration_close_date = close_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            settings.timezone = tz_name
            settings.banner_text = banner_text.strip()
            settings.contact_email = contact_email.strip()
            settings.developer_contact = developer_contact.strip()
            settings.front_page_content = front_page_content.strip()
            settings.payment_instructions = payment_instructions.strip()
            settings.insurance_instructions = insurance_instructions.strip()
            settings.vendor_agreement_text = vendor_agreement_text.strip()
            try:
                fee_pct = float(processing_fee_percent)
                if not math.isfinite(fee_pct):
                    fee_pct = 0
                settings.processing_fee_percent = max(0, min(fee_pct, 50))
            except (ValueError, TypeError):
                settings.processing_fee_percent = 0
            try:
                flat_cents = int(processing_fee_flat_cents)
                settings.processing_fee_flat_cents = max(0, min(flat_cents, 10000))  # cap at $100
            except (ValueError, TypeError):
                settings.processing_fee_flat_cents = 0
            settings.refund_policy = refund_policy.strip()
            settings.refund_presets = refund_presets.strip() or "100,75,50,25,0"
            settings.notify_new_registration = notify_new_registration is not None
            settings.notify_payment_received = notify_payment_received is not None
            settings.notify_insurance_uploaded = notify_insurance_uploaded is not None

            # Payment deadline settings
            try:
                pdd = int(payment_deadline_days)
                pdd = max(1, min(pdd, 90))
            except (ValueError, TypeError):
                pdd = 7
            settings.payment_deadline_days = pdd

            try:
                r1d = int(reminder_1_days)
                r1d = max(1, min(r1d, 89))
            except (ValueError, TypeError):
                r1d = 2
            settings.reminder_1_days = r1d

            try:
                r2d = int(reminder_2_days)
                r2d = max(1, min(r2d, 89))
            except (ValueError, TypeError):
                r2d = 5
            settings.reminder_2_days = r2d

            # Validate reminder day constraints
            reminder_errors = settings.validate_reminder_days()
            if reminder_errors:
                flash = [{"category": "error", "text": e} for e in reminder_errors]
                return _template(request, "admin/settings.html", {
                    "settings": settings,
                    "admin_emails": ADMIN_EMAILS,
                    "get_flashed_messages": lambda: flash,
                }, session=session)

            settings.reminder_1_subject = reminder_1_subject.strip() or "Payment Reminder — {event_name}"
            settings.reminder_1_body = reminder_1_body
            settings.reminder_2_subject = reminder_2_subject.strip() or "Urgent: Payment Deadline Approaching — {event_name}"
            settings.reminder_2_body = reminder_2_body

            db.commit()
            invalidate_event_settings_cache(db)
            request.app.state.event_name = settings.event_name
            request.app.state.event_timezone = settings.timezone
        except ValueError:
            flash = [{"category": "error", "text": "Invalid date format. Please use YYYY-MM-DD."}]
            return _template(request, "admin/settings.html", {
                "settings": settings,
                "admin_emails": ADMIN_EMAILS,
                "get_flashed_messages": lambda: flash,
            }, session=session)
    return RedirectResponse(url="/admin/settings", status_code=303)


# --- FAQ ---

@router.get("/faq", response_class=HTMLResponse)
async def faq_page(
    request: Request,
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    settings = get_event_settings(db)
    return _template(request, "admin/faq.html", {
        "developer_contact": settings.developer_contact if settings else "",
    }, session=session)


# --- CSV Export ---

@router.get("/export")
async def export_csv(
    session: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    registrations = db.query(Registration).order_by(Registration.created_at.desc()).all()
    booth_types = {bt.id: bt.name for bt in db.query(BoothType).all()}
    insurance_docs = {doc.email: doc for doc in db.query(InsuranceDocument).all()}
    all_notes = db.query(AdminNote).order_by(AdminNote.created_at.asc()).all()
    notes_by_reg: dict[str, list[str]] = {}
    for n in all_notes:
        notes_by_reg.setdefault(n.registration_id, []).append(
            f"{n.admin_email} {n.created_at.strftime('%m/%d')}: {n.text}"
        )

    def _sanitize_csv(value: str) -> str:
        """Prevent CSV formula injection by prefixing dangerous characters."""
        if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + value
        return value

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Registration ID", "Status", "Business Name", "Contact Name",
        "Email", "Phone", "Category", "Description",
        "Booth Type", "Electrical Equipment", "Electrical Other",
        "Insurance", "Amount Paid", "Processing Fee", "Refund Amount",
        "Stripe Payment Intent ID", "Created At", "Approved At",
        "Rejected At", "Cancelled At", "Withdrawn At", "Reversal Reason",
        "Concern Status", "Admin Notes",
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
            _sanitize_csv(reg.business_name),
            _sanitize_csv(reg.contact_name),
            reg.email,
            reg.phone,
            CATEGORIES.get(reg.category, reg.category),
            _sanitize_csv(reg.description),
            booth_types.get(reg.booth_type_id, "Unknown"),
            _sanitize_csv(reg.electrical_equipment or ""),
            _sanitize_csv(reg.electrical_other or ""),
            insurance_status,
            f"${reg.amount_paid / 100:.2f}" if reg.amount_paid else "",
            f"${reg.processing_fee / 100:.2f}" if reg.processing_fee else "",
            f"${reg.refund_amount / 100:.2f}" if reg.refund_amount else "",
            reg.stripe_payment_intent_id or "",
            reg.created_at.strftime("%Y-%m-%d %H:%M") if reg.created_at else "",
            reg.approved_at.strftime("%Y-%m-%d %H:%M") if reg.approved_at else "",
            reg.rejected_at.strftime("%Y-%m-%d %H:%M") if reg.rejected_at else "",
            reg.cancelled_at.strftime("%Y-%m-%d %H:%M") if reg.cancelled_at else "",
            reg.withdrawn_at.strftime("%Y-%m-%d %H:%M") if reg.withdrawn_at else "",
            _sanitize_csv(reg.reversal_reason or ""),
            reg.concern_status or "none",
            _sanitize_csv(" | ".join(notes_by_reg.get(reg.registration_id, []))),
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
