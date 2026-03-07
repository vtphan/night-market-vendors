import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from sqlalchemy.orm import Session

from app.database import get_db, get_event_settings
from app.csrf import generate_csrf_token, require_csrf
from app.session import read_session, require_vendor, get_client_ip
from app.models import Registration, BoothType, InsuranceDocument, RegistrationDraft
from app.services.registration import (
    create_registration,
    check_submission_rate_limit,
    get_inventory,
    get_waitlist_position,
    transition_status,
    try_cancel_active_payment_intent,
    LOW_INVENTORY_THRESHOLD,
    CATEGORIES,
)
from app.services.food_permit import FOOD_CATEGORIES, PERMITS_DIR
from app.services.email import (
    send_submission_confirmation_email,
    send_admin_notification_email,
    send_withdrawal_confirmation_email,
)
from app.services.payment import create_payment_intent, calculate_processing_fee
from app.config import STRIPE_PUBLISHABLE_KEY, APP_URL
from app.upload_constants import ALLOWED_EXTENSIONS, ALLOWED_CONTENT_TYPES, MAX_FILE_SIZE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vendor", tags=["vendor"])


def _template(request, name, ctx):
    """Render a template with standard context."""
    session = read_session(request)
    ctx.setdefault("request", request)
    ctx.setdefault("session", session)
    ctx.setdefault("csrf_token", generate_csrf_token())
    ctx.setdefault("get_flashed_messages", lambda: [])
    return request.app.state.templates.TemplateResponse(name, ctx)


def _get_draft(db: Session, email: str) -> dict:
    """Get registration draft from database."""
    row = db.query(RegistrationDraft).filter(RegistrationDraft.email == email).first()
    if row:
        return json.loads(row.draft_json)
    return {}


def _upsert_draft(db: Session, email: str, draft: dict) -> None:
    """Insert or update a registration draft in the database."""
    row = db.query(RegistrationDraft).filter(RegistrationDraft.email == email).first()
    if row:
        row.draft_json = json.dumps(draft)
        row.updated_at = datetime.now(timezone.utc)
    else:
        row = RegistrationDraft(email=email, draft_json=json.dumps(draft))
        db.add(row)
    db.commit()


def _delete_draft(db: Session, email: str) -> None:
    """Delete a registration draft from the database."""
    db.query(RegistrationDraft).filter(RegistrationDraft.email == email).delete()
    db.commit()


# --- Registration gateway ---

@router.get("/register", response_class=HTMLResponse)
async def register_gateway(request: Request, edit: str = "", new: str = "", db: Session = Depends(get_db)):
    settings = get_event_settings(db)
    now = datetime.now(timezone.utc)

    # Check if registration is open
    if settings and not settings.is_registration_open():
        if now < settings.registration_open_date.replace(tzinfo=timezone.utc):
            return _template(request, "vendor/coming_soon.html", {
                "open_date": settings.registration_open_date,
            })
        else:
            return _template(request, "vendor/registration_closed.html", {})

    # Must be logged in as vendor
    session = read_session(request)
    if not session or session.get("user_type") != "vendor":
        return RedirectResponse(url="/auth/login", status_code=303)

    email = session.get("email", "")

    # If new=1, clear old draft and start fresh
    if new == "1":
        _delete_draft(db, email)
        return RedirectResponse(url="/vendor/register", status_code=303)

    # Determine which step to show based on draft
    draft = _get_draft(db, email)
    step = draft.get("current_step", 1)

    # If edit=1 query param, go back to step 1 form with draft data
    if edit == "1":
        step = 1

    booth_types = (
        db.query(BoothType)
        .filter(BoothType.is_active == True)
        .order_by(BoothType.sort_order)
        .all()
    )
    inventory = get_inventory(db)
    booth_availability = {item["id"]: item["available"] for item in inventory}

    if step == 2:
        booth_type = db.query(BoothType).filter(BoothType.id == draft.get("booth_type_id")).first()
        return _template(request, "vendor/register_step2.html", {
            "draft": draft,
            "booth_type": booth_type,
            "agreement_text": settings.vendor_agreement_text if settings else "",
            "insurance_instructions": settings.insurance_instructions if settings else "",
            "settings": settings,
        })

    # Default: step 1 (also handles step 0 or any unexpected value)
    return _template(request, "vendor/register_step1.html", {
        "booth_types": booth_types,
        "booth_availability": booth_availability,
        "LOW_INVENTORY_THRESHOLD": LOW_INVENTORY_THRESHOLD,
        "draft": draft,
        "email": email,
        "settings": settings,
    })


# --- Step 1: All registration info ---

@router.post("/register/step1")
async def register_step1(
    request: Request,
    contact_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    business_name: str = Form(...),
    category: str = Form(...),
    description: str = Form(...),
    booth_type_id: int | None = Form(None),
    address: str = Form(""),
    city_state_zip: str = Form(""),
    electrical_equipment: list[str] = Form([]),
    electrical_other: str = Form(""),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    session = read_session(request)
    if not session or session.get("user_type") != "vendor":
        return RedirectResponse(url="/auth/login", status_code=303)

    # Block submissions if registration is closed
    settings = get_event_settings(db)
    if settings and not settings.is_registration_open():
        return RedirectResponse(url="/vendor/register", status_code=303)

    # Force email from session — ignore form value
    email = session.get("email", "").lower().strip()
    errors = []

    MAX_LENGTHS = {"contact_name": 200, "business_name": 200, "phone": 30, "description": 2000, "electrical_other": 500}

    if not contact_name.strip():
        errors.append("Full name is required.")
    elif len(contact_name) > MAX_LENGTHS["contact_name"]:
        errors.append(f"Full name must be {MAX_LENGTHS['contact_name']} characters or less.")
    if not phone.strip():
        errors.append("Phone number is required.")
    elif len(phone) > MAX_LENGTHS["phone"]:
        errors.append(f"Phone number must be {MAX_LENGTHS['phone']} characters or less.")
    if not business_name.strip():
        errors.append("Business name is required.")
    elif len(business_name) > MAX_LENGTHS["business_name"]:
        errors.append(f"Business name must be {MAX_LENGTHS['business_name']} characters or less.")
    if category not in CATEGORIES:
        errors.append("Please select a valid category.")
    if not description.strip():
        errors.append("Description is required.")
    elif len(description) > MAX_LENGTHS["description"]:
        errors.append(f"Description must be {MAX_LENGTHS['description']} characters or less.")
    if len(electrical_other) > MAX_LENGTHS["electrical_other"]:
        errors.append(f"Electrical other must be {MAX_LENGTHS['electrical_other']} characters or less.")

    # Address required for food/beverage (food permit)
    if category in ("food", "beverage"):
        if not address.strip():
            errors.append("Street address is required for food/beverage vendors (needed for food permit).")
        elif len(address) > 300:
            errors.append("Street address must be 300 characters or less.")
        if not city_state_zip.strip():
            errors.append("City, State, ZIP is required for food/beverage vendors (needed for food permit).")
        elif len(city_state_zip) > 200:
            errors.append("City, State, ZIP must be 200 characters or less.")

    # Validate booth type
    booth_type = None
    if booth_type_id is None:
        errors.append("Please select a booth type.")
    else:
        booth_type = db.query(BoothType).filter(BoothType.id == booth_type_id, BoothType.is_active == True).first()
        if not booth_type:
            errors.append("Please select a valid booth type.")

    if errors:
        flash = [{"category": "error", "text": e} for e in errors]
        settings = get_event_settings(db)
        booth_types = db.query(BoothType).filter(BoothType.is_active == True).order_by(BoothType.sort_order).all()
        inventory = get_inventory(db)
        booth_availability = {item["id"]: item["available"] for item in inventory}
        # Preserve form values in draft for re-display
        form_draft = {
            "contact_name": contact_name,
            "phone": phone,
            "business_name": business_name,
            "category": category,
            "description": description,
            "address": address,
            "city_state_zip": city_state_zip,
            "electrical_equipment": electrical_equipment,
            "electrical_other": electrical_other,
            "booth_type_id": booth_type_id,
        }
        return _template(request, "vendor/register_step1.html", {
            "booth_types": booth_types,
            "booth_availability": booth_availability,
            "LOW_INVENTORY_THRESHOLD": LOW_INVENTORY_THRESHOLD,
            "draft": form_draft,
            "email": email,
            "settings": settings,
            "get_flashed_messages": lambda: flash,
        })

    draft = {
        "current_step": 2,
        "contact_name": contact_name.strip(),
        "email": email,
        "phone": phone.strip(),
        "business_name": business_name.strip(),
        "category": category,
        "description": description.strip(),
        "address": address.strip(),
        "city_state_zip": city_state_zip.strip(),
        "electrical_equipment": ",".join(electrical_equipment) if electrical_equipment else "",
        "electrical_other": electrical_other.strip(),
        "booth_type_id": booth_type.id,
        "booth_type_name": booth_type.name,
        "booth_type_price": booth_type.price,
    }

    _upsert_draft(db, email, draft)
    return RedirectResponse(url="/vendor/register", status_code=303)


# --- Final submit ---

@router.post("/register/submit")
async def register_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    session = read_session(request)
    if not session or session.get("user_type") != "vendor":
        return RedirectResponse(url="/vendor/register", status_code=303)

    # Block submissions if registration is closed
    settings_check = get_event_settings(db)
    if settings_check and not settings_check.is_registration_open():
        return RedirectResponse(url="/vendor/register", status_code=303)

    email = session.get("email", "").lower().strip()
    draft = _get_draft(db, email)

    # Validate all required fields are present
    required = ["email", "contact_name", "business_name", "phone", "category", "description", "booth_type_id"]
    if not all(draft.get(k) for k in required):
        return RedirectResponse(url="/vendor/register", status_code=303)

    # Re-validate booth type is still active and category is still valid
    if draft["category"] not in CATEGORIES:
        return RedirectResponse(url="/vendor/register?edit=1", status_code=303)
    booth_type_check = db.query(BoothType).filter(
        BoothType.id == draft["booth_type_id"],
        BoothType.is_active == True,
    ).first()
    if not booth_type_check:
        return RedirectResponse(url="/vendor/register?edit=1", status_code=303)

    # Rate limit check
    ip = get_client_ip(request)
    if not check_submission_rate_limit(db, ip):
        flash = [{"category": "error", "text": "Too many submissions. Please try again later."}]
        booth_type = db.query(BoothType).filter(BoothType.id == draft.get("booth_type_id")).first()
        return _template(request, "vendor/register_step2.html", {
            "draft": draft,
            "booth_type": booth_type,
            "agreement_text": settings_check.vendor_agreement_text if settings_check else "",
            "insurance_instructions": settings_check.insurance_instructions if settings_check else "",
            "settings": settings_check,
            "get_flashed_messages": lambda: flash,
        })

    # Create registration
    data = {
        "email": draft["email"],
        "business_name": draft["business_name"],
        "contact_name": draft["contact_name"],
        "phone": draft["phone"],
        "category": draft["category"],
        "description": draft["description"],
        "address": draft.get("address") or None,
        "city_state_zip": draft.get("city_state_zip") or None,
        "electrical_equipment": draft.get("electrical_equipment") or None,
        "electrical_other": draft.get("electrical_other") or None,
        "booth_type_id": draft["booth_type_id"],
        "agreement_accepted_at": datetime.now(timezone.utc),
        "agreement_ip_address": get_client_ip(request),
    }

    registration = create_registration(db, data)

    # Send confirmation email in background
    booth_type = db.query(BoothType).filter(BoothType.id == draft["booth_type_id"]).first()
    background_tasks.add_task(
        send_submission_confirmation_email,
        draft["email"],
        registration.registration_id,
        booth_type.name if booth_type else "Unknown",
    )

    # Admin notification
    settings = get_event_settings(db)
    if settings and settings.notify_new_registration:
        background_tasks.add_task(
            send_admin_notification_email,
            "new_registration",
            registration.registration_id,
            draft["business_name"],
            f"{APP_URL}/admin/registrations/{registration.registration_id}",
        )

    # Clear draft from database
    _delete_draft(db, email)
    return RedirectResponse(
        url=f"/vendor/confirm/{registration.registration_id}",
        status_code=303,
    )


# --- Discard draft ---

@router.post("/register/discard")
async def register_discard(
    request: Request,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    session = read_session(request)
    if not session or session.get("user_type") != "vendor":
        return RedirectResponse(url="/auth/login", status_code=303)
    _delete_draft(db, session["email"])
    return RedirectResponse(url="/vendor/dashboard", status_code=303)


# --- Confirmation page ---

@router.get("/confirm/{registration_id}", response_class=HTMLResponse)
async def confirmation_page(
    request: Request,
    registration_id: str,
    session: dict = Depends(require_vendor),
    db: Session = Depends(get_db),
):
    registration = (
        db.query(Registration)
        .filter(
            Registration.registration_id == registration_id,
            Registration.email == session["email"],
        )
        .first()
    )
    if not registration:
        return RedirectResponse(url="/vendor/register", status_code=303)

    booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()
    return _template(request, "vendor/confirmation.html", {
        "registration": registration,
        "booth_type": booth_type,
    })


# --- Registration detail ---

@router.get("/registration/{registration_id}", response_class=HTMLResponse)
async def registration_detail(
    request: Request,
    registration_id: str,
    session: dict = Depends(require_vendor),
    db: Session = Depends(get_db),
):
    registration = (
        db.query(Registration)
        .filter(
            Registration.registration_id == registration_id,
            Registration.email == session["email"],
        )
        .first()
    )
    if not registration:
        return RedirectResponse(url="/vendor/dashboard", status_code=303)

    booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()

    settings = get_event_settings(db)
    insurance_doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == session["email"]).first()

    # Use the price locked at approval time when available so admin price
    # changes don't retroactively affect this vendor's displayed/charged amount.
    booth_price = registration.approved_price if registration.approved_price is not None else (booth_type.price if booth_type else 0)

    needs_permit = registration.category in FOOD_CATEGORIES
    food_permit_available = needs_permit and (PERMITS_DIR / f"{registration.registration_id}.pdf").exists()

    ctx = {
        "registration": registration,
        "booth_type": booth_type,
        "booth_price": booth_price,
        "settings": settings,
        "waitlist_position": get_waitlist_position(db, registration),
        "insurance_doc": insurance_doc,
        "needs_permit": needs_permit,
        "food_permit_available": food_permit_available,
        "now": datetime.now(timezone.utc),
    }
    if registration.status == "approved":
        ctx["stripe_publishable_key"] = STRIPE_PUBLISHABLE_KEY
        fee_percent = settings.processing_fee_percent if settings else 0
        fee_flat = settings.processing_fee_flat_cents if settings else 0
        ctx["processing_fee_cents"] = calculate_processing_fee(booth_price, fee_percent, fee_flat)

    return _template(request, "vendor/registration_detail.html", ctx)


# --- Food permit download ---

@router.get("/registrations/{registration_id}/food-permit")
async def download_food_permit(
    registration_id: str,
    session: dict = Depends(require_vendor),
    db: Session = Depends(get_db),
):
    registration = (
        db.query(Registration)
        .filter(
            Registration.registration_id == registration_id,
            Registration.email == session["email"],
        )
        .first()
    )
    if not registration or registration.status not in ("approved", "paid"):
        return RedirectResponse(url=f"/vendor/registrations/{registration_id}" if registration else "/vendor/dashboard", status_code=303)

    permit_path = PERMITS_DIR / f"{registration_id}.pdf"
    if not permit_path.exists():
        return RedirectResponse(url=f"/vendor/registrations/{registration_id}", status_code=303)

    return FileResponse(
        path=str(permit_path),
        media_type="application/pdf",
        filename=f"food_permit_{registration_id}.pdf",
    )


# --- Payment ---

@router.post("/registration/{registration_id}/pay")
async def create_payment(
    request: Request,
    registration_id: str,
    session: dict = Depends(require_vendor),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    registration = (
        db.query(Registration)
        .filter(
            Registration.registration_id == registration_id,
            Registration.email == session["email"],
        )
        .with_for_update()
        .first()
    )
    if not registration:
        return JSONResponse(status_code=404, content={"error": "Registration not found"})

    if registration.status != "approved":
        return JSONResponse(status_code=400, content={"error": "Registration is not approved for payment"})

    booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()
    if not booth_type:
        return JSONResponse(status_code=400, content={"error": "Booth type not found"})

    # Use the price locked at approval time so admin price changes don't
    # retroactively affect this vendor's payment amount.
    price = registration.approved_price if registration.approved_price is not None else booth_type.price
    settings = get_event_settings(db)
    fee_percent = settings.processing_fee_percent if settings else 0
    fee_flat = settings.processing_fee_flat_cents if settings else 0
    processing_fee_cents = calculate_processing_fee(price, fee_percent, fee_flat)

    try:
        client_secret = create_payment_intent(db, registration, booth_type, processing_fee_cents)
        db.commit()
    except ValueError as e:
        db.rollback()
        logger.info("Payment blocked for %s: %s", registration_id, e)
        return JSONResponse(
            status_code=409,
            content={"error": str(e)},
        )
    except Exception:
        db.rollback()
        logger.exception("Stripe PaymentIntent creation failed for %s", registration_id)
        return JSONResponse(
            status_code=502,
            content={"error": "Payment service is temporarily unavailable. Please try again in a few minutes."},
        )

    return JSONResponse(content={
        "client_secret": client_secret,
        "amount": price + processing_fee_cents,
        "booth_price": price,
        "processing_fee": processing_fee_cents,
        "booth_type": booth_type.name,
    })


# --- Vendor withdrawal ---

@router.post("/registration/{registration_id}/withdraw")
async def withdraw_registration(
    request: Request,
    registration_id: str,
    background_tasks: BackgroundTasks,
    reason: str = Form(""),
    session: dict = Depends(require_vendor),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    registration = (
        db.query(Registration)
        .filter(
            Registration.registration_id == registration_id,
            Registration.email == session["email"],
        )
        .first()
    )
    if not registration:
        return RedirectResponse(url="/vendor/dashboard", status_code=303)

    if registration.status not in ("pending", "approved"):
        return RedirectResponse(url=f"/vendor/registration/{registration_id}", status_code=303)

    # If approved with a Stripe PI, try to cancel it first
    if registration.status == "approved" and registration.stripe_payment_intent_id:
        ok, msg = try_cancel_active_payment_intent(registration)
        if not ok:
            flash = [{"category": "error", "text": f"Cannot withdraw: {msg}"}]
            booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()
            settings = get_event_settings(db)
            insurance_doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == session["email"]).first()
            booth_price = registration.approved_price if registration.approved_price is not None else (booth_type.price if booth_type else 0)
            return _template(request, "vendor/registration_detail.html", {
                "registration": registration,
                "booth_type": booth_type,
                "booth_price": booth_price,
                "settings": settings,
                "waitlist_position": get_waitlist_position(db, registration),
                "insurance_doc": insurance_doc,
                "get_flashed_messages": lambda: flash,
            })

    reason = reason.strip()
    if reason:
        reason = f"{reason} \u2014 {registration.contact_name} ({session['email']})"
    transition_status(db, registration, "withdrawn", reversal_reason=reason or None)

    booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()
    background_tasks.add_task(
        send_withdrawal_confirmation_email,
        session["email"],
        registration_id,
        booth_type.name if booth_type else "Unknown",
    )
    background_tasks.add_task(
        send_admin_notification_email,
        "vendor_withdrawal",
        registration_id,
        registration.business_name,
        f"{APP_URL}/admin/registrations/{registration_id}",
    )

    return RedirectResponse(url="/vendor/dashboard", status_code=303)


# --- Vendor dashboard ---

@router.get("/dashboard", response_class=HTMLResponse)
async def vendor_dashboard(
    request: Request,
    session: dict = Depends(require_vendor),
    db: Session = Depends(get_db),
):
    registrations = (
        db.query(Registration)
        .filter(Registration.email == session["email"])
        .order_by(Registration.created_at.desc())
        .all()
    )

    # Attach booth type names and waitlist positions
    booth_types = {bt.id: bt for bt in db.query(BoothType).all()}
    reg_data = []
    for reg in registrations:
        reg_data.append({
            "registration": reg,
            "booth_type": booth_types.get(reg.booth_type_id),
            "waitlist_position": get_waitlist_position(db, reg),
        })

    settings = get_event_settings(db)
    registration_open = settings.is_registration_open() if settings else False
    insurance_doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == session["email"]).first()

    # Build "Needs Attention" items — payment alerts only
    needs_attention = []

    # Approved registrations awaiting payment
    now = datetime.now(timezone.utc)
    for item in reg_data:
        if item["registration"].status == "approved":
            reg = item["registration"]
            deadline = reg.payment_deadline
            deadline_date = None
            overdue = False
            if deadline:
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                deadline_date = deadline.strftime("%b %d, %Y")
                overdue = now > deadline
            needs_attention.append({
                "type": "payment",
                "message": f"{reg.business_name} — approved, awaiting payment",
                "link": f"/vendor/registration/{reg.registration_id}",
                "link_text": "Pay now",
                "deadline_date": deadline_date,
                "overdue": overdue,
            })

    return _template(request, "vendor/dashboard.html", {
        "registrations": reg_data,
        "registration_open": registration_open,
        "settings": settings,
        "insurance_doc": insurance_doc,
        "needs_attention": needs_attention,
    })


# --- Vendor FAQ ---

@router.get("/faq", response_class=HTMLResponse)
async def vendor_faq(request: Request, db: Session = Depends(get_db)):
    settings = get_event_settings(db)
    return _template(request, "vendor/faq.html", {
        "settings": settings,
        "registration_open": settings.is_registration_open() if settings else False,
    })


# --- Insurance ---

@router.get("/insurance", response_class=HTMLResponse)
async def insurance_page(
    request: Request,
    session: dict = Depends(require_vendor),
    db: Session = Depends(get_db),
):
    insurance_doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == session["email"]).first()
    settings = get_event_settings(db)
    return _template(request, "vendor/insurance.html", {
        "insurance_doc": insurance_doc,
        "settings": settings,
    })


@router.post("/insurance/upload")
async def insurance_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session: dict = Depends(require_vendor),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    email = session["email"]
    uploads_dir: Path = request.app.state.uploads_dir

    # Validate extension
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        flash = [{"category": "error", "text": f"File type not allowed. Please upload a PDF, PNG, or JPG file."}]
        insurance_doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == email).first()
        settings = get_event_settings(db)
        return _template(request, "vendor/insurance.html", {
            "insurance_doc": insurance_doc,
            "settings": settings,
            "get_flashed_messages": lambda: flash,
        })

    # Validate content type
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        flash = [{"category": "error", "text": "File type not allowed. Please upload a PDF, PNG, or JPG file."}]
        insurance_doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == email).first()
        settings = get_event_settings(db)
        return _template(request, "vendor/insurance.html", {
            "insurance_doc": insurance_doc,
            "settings": settings,
            "get_flashed_messages": lambda: flash,
        })

    # Read file in chunks to avoid holding unbounded data in memory
    chunks = []
    total_size = 0
    while True:
        chunk = await file.read(64 * 1024)  # 64 KB chunks
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > MAX_FILE_SIZE:
            break
        chunks.append(chunk)
    contents = b"".join(chunks)

    if total_size > MAX_FILE_SIZE:
        flash = [{"category": "error", "text": "File is too large. Maximum size is 10 MB."}]
        insurance_doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == email).first()
        settings = get_event_settings(db)
        return _template(request, "vendor/insurance.html", {
            "insurance_doc": insurance_doc,
            "settings": settings,
            "get_flashed_messages": lambda: flash,
        })

    stored_filename = f"{uuid4().hex}{ext}"
    file_path = uploads_dir / stored_filename

    # Check for existing document — replace it
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

    # Write new file to disk, then commit. If commit fails, clean up new file.
    with open(file_path, "wb") as f:
        f.write(contents)

    try:
        db.commit()
    except Exception:
        # Remove the newly written file since the DB commit failed
        if file_path.exists():
            file_path.unlink()
        raise

    # Delete old file only after successful commit
    if old_stored_filename:
        old_path = uploads_dir / old_stored_filename
        if old_path.exists():
            old_path.unlink()

    # Admin notification for insurance upload
    settings = get_event_settings(db)
    if settings and settings.notify_insurance_uploaded:
        # Find an active registration for this vendor to link in the notification
        reg = db.query(Registration).filter(
            Registration.email == email,
            Registration.status.in_(["pending", "approved", "paid"]),
        ).first()
        if reg:
            background_tasks.add_task(
                send_admin_notification_email,
                "insurance_uploaded",
                reg.registration_id,
                reg.business_name,
                f"{APP_URL}/admin/registrations/{reg.registration_id}",
            )

    return RedirectResponse(url="/vendor/insurance", status_code=303)


@router.get("/insurance/file/{stored_filename}")
async def insurance_file(
    request: Request,
    stored_filename: str,
    session: dict = Depends(require_vendor),
    db: Session = Depends(get_db),
):
    if ".." in stored_filename or "/" in stored_filename or "\\" in stored_filename:
        return RedirectResponse(url="/vendor/insurance", status_code=303)

    doc = db.query(InsuranceDocument).filter(InsuranceDocument.stored_filename == stored_filename).first()
    if not doc or doc.email != session["email"]:
        return RedirectResponse(url="/vendor/insurance", status_code=303)

    uploads_dir = request.app.state.uploads_dir
    file_path = (uploads_dir / stored_filename).resolve()
    if not file_path.is_relative_to(uploads_dir.resolve()):
        return RedirectResponse(url="/vendor/insurance", status_code=303)
    if not file_path.exists():
        return RedirectResponse(url="/vendor/insurance", status_code=303)

    return FileResponse(
        path=str(file_path),
        media_type=doc.content_type,
        filename=doc.original_filename,
    )
