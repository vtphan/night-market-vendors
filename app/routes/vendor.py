import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.csrf import generate_csrf_token, require_csrf
from app.session import read_session, update_session_data, require_vendor
from app.models import Registration, BoothType, EventSettings, InsuranceDocument
from app.services.registration import (
    create_registration,
    check_submission_rate_limit,
    get_inventory,
    get_waitlist_position,
    LOW_INVENTORY_THRESHOLD,
    CATEGORIES,
)
from app.services.email import send_submission_confirmation_email
from app.services.payment import create_payment_intent
from app.config import STRIPE_PUBLISHABLE_KEY

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vendor", tags=["vendor"])

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
ALLOWED_CONTENT_TYPES = {"application/pdf", "image/png", "image/jpeg"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def _template(request, name, ctx):
    """Render a template with standard context."""
    session = read_session(request)
    ctx.setdefault("request", request)
    ctx.setdefault("session", session)
    ctx.setdefault("csrf_token", generate_csrf_token())
    ctx.setdefault("get_flashed_messages", lambda: [])
    return request.app.state.templates.TemplateResponse(name, ctx)


def _get_draft(request):
    """Get registration draft from session cookie."""
    session = read_session(request)
    if session:
        return session.get("registration_draft") or {}
    return {}


# --- Registration gateway ---

@router.get("/register", response_class=HTMLResponse)
async def register_gateway(request: Request, edit: str = "", new: str = "", db: Session = Depends(get_db)):
    settings = db.query(EventSettings).first()
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
        response = RedirectResponse(url="/vendor/register", status_code=303)
        update_session_data(response, session, "registration_draft", None)
        return response

    # Determine which step to show based on draft
    draft = _get_draft(request)
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

    if step == 1 or step == 0:
        return _template(request, "vendor/register_step1.html", {
            "agreement_text": settings.vendor_agreement_text if settings else "",
            "insurance_instructions": settings.insurance_instructions if settings else "",
            "booth_types": booth_types,
            "booth_availability": booth_availability,
            "LOW_INVENTORY_THRESHOLD": LOW_INVENTORY_THRESHOLD,
            "draft": draft,
            "email": email,
        })
    elif step == 2:
        booth_type = db.query(BoothType).filter(BoothType.id == draft.get("booth_type_id")).first()
        return _template(request, "vendor/register_step2.html", {
            "draft": draft,
            "booth_type": booth_type,
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
    electrical_equipment: list[str] = Form([]),
    electrical_other: str = Form(""),
    agreement_accepted: str = Form(""),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    session = read_session(request)
    if not session or session.get("user_type") != "vendor":
        return RedirectResponse(url="/auth/login", status_code=303)

    # Force email from session — ignore form value
    email = session.get("email", "").lower().strip()
    errors = []

    if agreement_accepted != "yes":
        errors.append("You must accept the vendor agreement to continue.")
    if not contact_name.strip():
        errors.append("Full name is required.")
    if not phone.strip():
        errors.append("Phone number is required.")
    if not business_name.strip():
        errors.append("Business name is required.")
    if category not in CATEGORIES:
        errors.append("Please select a valid category.")
    if not description.strip():
        errors.append("Description is required.")

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
        settings = db.query(EventSettings).first()
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
            "electrical_equipment": electrical_equipment,
            "electrical_other": electrical_other,
            "booth_type_id": booth_type_id,
        }
        return _template(request, "vendor/register_step1.html", {
            "agreement_text": settings.vendor_agreement_text if settings else "",
            "insurance_instructions": settings.insurance_instructions if settings else "",
            "booth_types": booth_types,
            "booth_availability": booth_availability,
            "LOW_INVENTORY_THRESHOLD": LOW_INVENTORY_THRESHOLD,
            "draft": form_draft,
            "email": email,
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
        "electrical_equipment": ",".join(electrical_equipment) if electrical_equipment else "",
        "electrical_other": electrical_other.strip(),
        "booth_type_id": booth_type.id,
        "booth_type_name": booth_type.name,
        "booth_type_price": booth_type.price,
        "agreement_ip": request.client.host if request.client else "unknown",
        "agreement_accepted_at": datetime.now(timezone.utc).isoformat(),
    }

    response = RedirectResponse(url="/vendor/register", status_code=303)
    update_session_data(response, session, "registration_draft", draft)
    return response


# --- Final submit ---

@router.post("/register/submit")
async def register_submit(
    request: Request,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    session = read_session(request)
    if not session:
        return RedirectResponse(url="/vendor/register", status_code=303)

    draft = session.get("registration_draft", {})

    # Validate all required fields are present
    required = ["email", "contact_name", "business_name", "phone", "category", "description", "booth_type_id"]
    if not all(draft.get(k) for k in required):
        return RedirectResponse(url="/vendor/register", status_code=303)

    # Rate limit check
    ip = request.client.host if request.client else "unknown"
    if not check_submission_rate_limit(ip):
        flash = [{"category": "error", "text": "Too many submissions. Please try again later."}]
        booth_type = db.query(BoothType).filter(BoothType.id == draft.get("booth_type_id")).first()
        return _template(request, "vendor/register_step2.html", {
            "draft": draft,
            "booth_type": booth_type,
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
        "electrical_equipment": draft.get("electrical_equipment") or None,
        "electrical_other": draft.get("electrical_other") or None,
        "booth_type_id": draft["booth_type_id"],
        "agreement_accepted_at": datetime.fromisoformat(draft["agreement_accepted_at"]),
        "agreement_ip_address": draft.get("agreement_ip", "unknown"),
    }

    registration = create_registration(db, data)

    # Send confirmation email (non-blocking)
    booth_type = db.query(BoothType).filter(BoothType.id == draft["booth_type_id"]).first()
    send_submission_confirmation_email(
        draft["email"],
        registration.registration_id,
        booth_type.name if booth_type else "Unknown",
    )

    # Clear draft from session
    response = RedirectResponse(
        url=f"/vendor/confirm/{registration.registration_id}",
        status_code=303,
    )
    update_session_data(response, session, "registration_draft", None)
    return response


# --- Confirmation page ---

@router.get("/confirm/{registration_id}", response_class=HTMLResponse)
async def confirmation_page(
    request: Request,
    registration_id: str,
    db: Session = Depends(get_db),
):
    registration = (
        db.query(Registration)
        .filter(Registration.registration_id == registration_id)
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

    settings = db.query(EventSettings).first()
    insurance_doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == session["email"]).first()

    ctx = {
        "registration": registration,
        "booth_type": booth_type,
        "settings": settings,
        "waitlist_position": get_waitlist_position(db, registration),
        "insurance_doc": insurance_doc,
    }
    if registration.status == "approved":
        ctx["stripe_publishable_key"] = STRIPE_PUBLISHABLE_KEY

    return _template(request, "vendor/registration_detail.html", ctx)


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
        .first()
    )
    if not registration:
        return JSONResponse(status_code=404, content={"error": "Registration not found"})

    if registration.status != "approved":
        return JSONResponse(status_code=400, content={"error": "Registration is not approved for payment"})

    booth_type = db.query(BoothType).filter(BoothType.id == registration.booth_type_id).first()
    if not booth_type:
        return JSONResponse(status_code=400, content={"error": "Booth type not found"})

    try:
        client_secret = create_payment_intent(db, registration, booth_type)
    except Exception:
        logger.exception("Stripe PaymentIntent creation failed for %s", registration_id)
        return JSONResponse(
            status_code=502,
            content={"error": "Payment service is temporarily unavailable. Please try again in a few minutes."},
        )

    return JSONResponse(content={
        "client_secret": client_secret,
        "amount": booth_type.price,
        "booth_type": booth_type.name,
    })


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

    settings = db.query(EventSettings).first()
    registration_open = settings.is_registration_open() if settings else False
    insurance_doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == session["email"]).first()

    # Build "Needs Attention" items for the vendor
    needs_attention = []
    has_active = any(
        r["registration"].status in ("pending", "approved", "paid")
        for r in reg_data
    )

    # Approved registrations awaiting payment
    for item in reg_data:
        if item["registration"].status == "approved":
            needs_attention.append({
                "type": "payment",
                "message": f"{item['registration'].business_name} — approved, awaiting payment",
                "link": f"/vendor/registration/{item['registration'].registration_id}",
                "link_text": "Pay now",
            })

    # Insurance: needs upload or pending review (only if vendor has active registrations)
    if has_active:
        if not insurance_doc:
            needs_attention.append({
                "type": "insurance",
                "message": "Insurance document required",
                "link": "/vendor/insurance",
                "link_text": "Upload now",
            })
        elif not insurance_doc.is_approved:
            needs_attention.append({
                "type": "insurance_pending",
                "message": "Insurance document uploaded — awaiting admin review",
                "link": "/vendor/insurance",
                "link_text": "View",
            })

    return _template(request, "vendor/dashboard.html", {
        "registrations": reg_data,
        "registration_open": registration_open,
        "settings": settings,
        "insurance_doc": insurance_doc,
        "needs_attention": needs_attention,
    })


# --- Insurance ---

@router.get("/insurance", response_class=HTMLResponse)
async def insurance_page(
    request: Request,
    session: dict = Depends(require_vendor),
    db: Session = Depends(get_db),
):
    insurance_doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == session["email"]).first()
    settings = db.query(EventSettings).first()
    return _template(request, "vendor/insurance.html", {
        "insurance_doc": insurance_doc,
        "settings": settings,
    })


@router.post("/insurance/upload")
async def insurance_upload(
    request: Request,
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
        settings = db.query(EventSettings).first()
        return _template(request, "vendor/insurance.html", {
            "insurance_doc": insurance_doc,
            "settings": settings,
            "get_flashed_messages": lambda: flash,
        })

    # Validate content type
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        flash = [{"category": "error", "text": "File type not allowed. Please upload a PDF, PNG, or JPG file."}]
        insurance_doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == email).first()
        settings = db.query(EventSettings).first()
        return _template(request, "vendor/insurance.html", {
            "insurance_doc": insurance_doc,
            "settings": settings,
            "get_flashed_messages": lambda: flash,
        })

    # Read file and validate size
    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        flash = [{"category": "error", "text": "File is too large. Maximum size is 10 MB."}]
        insurance_doc = db.query(InsuranceDocument).filter(InsuranceDocument.email == email).first()
        settings = db.query(EventSettings).first()
        return _template(request, "vendor/insurance.html", {
            "insurance_doc": insurance_doc,
            "settings": settings,
            "get_flashed_messages": lambda: flash,
        })

    stored_filename = f"{uuid4().hex}{ext}"
    file_path = uploads_dir / stored_filename

    # Check for existing document — replace it
    existing = db.query(InsuranceDocument).filter(InsuranceDocument.email == email).first()
    if existing:
        old_path = uploads_dir / existing.stored_filename
        if old_path.exists():
            old_path.unlink()
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

    # Write file to disk
    with open(file_path, "wb") as f:
        f.write(contents)

    db.commit()
    return RedirectResponse(url="/vendor/insurance", status_code=303)


@router.get("/insurance/file/{stored_filename}")
async def insurance_file(
    request: Request,
    stored_filename: str,
    session: dict = Depends(require_vendor),
    db: Session = Depends(get_db),
):
    doc = db.query(InsuranceDocument).filter(InsuranceDocument.stored_filename == stored_filename).first()
    if not doc or doc.email != session["email"]:
        return RedirectResponse(url="/vendor/insurance", status_code=303)

    file_path = request.app.state.uploads_dir / stored_filename
    if not file_path.exists():
        return RedirectResponse(url="/vendor/insurance", status_code=303)

    return FileResponse(
        path=str(file_path),
        media_type=doc.content_type,
        filename=doc.original_filename,
    )
