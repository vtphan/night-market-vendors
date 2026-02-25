import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.csrf import generate_csrf_token, require_csrf
from app.session import read_session, update_session_data, require_vendor
from app.models import Registration, BoothType, EventSettings
from app.services.registration import (
    create_registration,
    check_submission_rate_limit,
    CATEGORIES,
)
from app.services.email import send_submission_confirmation_email
from app.services.payment import create_payment_intent
from app.config import STRIPE_PUBLISHABLE_KEY

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

    if step == 1 or step == 0:
        return _template(request, "vendor/register_step1.html", {
            "agreement_text": settings.vendor_agreement_text if settings else "",
            "booth_types": booth_types,
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
    booth_type_id: int = Form(...),
    cuisine_type: str = Form(""),
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
    if category == "food" and not cuisine_type.strip():
        errors.append("Cuisine type is required for food vendors.")

    # Validate booth type
    booth_type = db.query(BoothType).filter(BoothType.id == booth_type_id, BoothType.is_active == True).first()
    if not booth_type:
        errors.append("Please select a valid booth type.")

    if errors:
        flash = [{"category": "error", "text": e} for e in errors]
        settings = db.query(EventSettings).first()
        booth_types = db.query(BoothType).filter(BoothType.is_active == True).order_by(BoothType.sort_order).all()
        # Preserve form values in draft for re-display
        form_draft = {
            "contact_name": contact_name,
            "phone": phone,
            "business_name": business_name,
            "category": category,
            "description": description,
            "cuisine_type": cuisine_type,
            "electrical_equipment": electrical_equipment,
            "electrical_other": electrical_other,
            "booth_type_id": booth_type_id,
        }
        return _template(request, "vendor/register_step1.html", {
            "agreement_text": settings.vendor_agreement_text if settings else "",
            "booth_types": booth_types,
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
        "cuisine_type": cuisine_type.strip() if category == "food" else "",
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
        "cuisine_type": draft.get("cuisine_type") or None,
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

    ctx = {
        "registration": registration,
        "booth_type": booth_type,
        "settings": settings,
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

    # Attach booth type names
    booth_types = {bt.id: bt for bt in db.query(BoothType).all()}
    reg_data = []
    for reg in registrations:
        reg_data.append({
            "registration": reg,
            "booth_type": booth_types.get(reg.booth_type_id),
        })

    settings = db.query(EventSettings).first()
    registration_open = settings.is_registration_open() if settings else False

    return _template(request, "vendor/dashboard.html", {
        "registrations": reg_data,
        "registration_open": registration_open,
        "settings": settings,
    })
