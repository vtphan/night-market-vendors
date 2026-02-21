import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.csrf import generate_csrf_token, require_csrf
from app.session import read_session, update_session_data, require_vendor
from app.models import Registration, BoothType, EventSettings
from app.services.registration import (
    create_registration,
    check_submission_rate_limit,
)
from app.services.email import send_submission_confirmation_email

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
        return session.get("registration_draft", {})
    return {}


# --- Registration gateway ---

@router.get("/register", response_class=HTMLResponse)
async def register_gateway(request: Request, db: Session = Depends(get_db)):
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

    # Check if user already has a session with an email
    session = read_session(request)
    if session and session.get("email"):
        existing = (
            db.query(Registration)
            .filter(Registration.email == session["email"])
            .first()
        )
        if existing:
            return RedirectResponse(url="/vendor/dashboard", status_code=303)

    # Determine which step to show based on draft
    draft = _get_draft(request)
    step = draft.get("current_step", 1)

    if step == 1:
        return _template(request, "vendor/register_step1.html", {
            "agreement_text": settings.vendor_agreement_text if settings else "",
        })
    elif step == 2:
        return _template(request, "vendor/register_step2.html", {"draft": draft})
    elif step == 3:
        booth_types = (
            db.query(BoothType)
            .filter(BoothType.is_active == True)
            .order_by(BoothType.sort_order)
            .all()
        )
        return _template(request, "vendor/register_step3.html", {
            "booth_types": booth_types,
            "draft": draft,
        })
    elif step == 4:
        booth_type = db.query(BoothType).filter(BoothType.id == draft.get("booth_type_id")).first()
        return _template(request, "vendor/register_step4.html", {
            "draft": draft,
            "booth_type": booth_type,
        })

    return _template(request, "vendor/register_step1.html", {
        "agreement_text": settings.vendor_agreement_text if settings else "",
    })


# --- Step 1: Agreement ---

@router.post("/register/step1")
async def register_step1(
    request: Request,
    contact_name: str = Form(...),
    email: str = Form(...),
    agreement_accepted: str = Form(...),
    _csrf: None = Depends(require_csrf),
):
    email = email.lower().strip()
    errors = []

    if agreement_accepted != "yes":
        errors.append("You must accept the vendor agreement to continue.")
    if not contact_name.strip():
        errors.append("Name is required.")
    if not email or "@" not in email:
        errors.append("A valid email address is required.")

    if errors:
        flash = [{"category": "error", "text": e} for e in errors]
        return _template(request, "vendor/register_step1.html", {
            "agreement_text": "",
            "get_flashed_messages": lambda: flash,
        })

    session = read_session(request)
    draft = {
        "current_step": 2,
        "contact_name": contact_name.strip(),
        "email": email,
        "agreement_ip": request.client.host if request.client else "unknown",
        "agreement_accepted_at": datetime.now(timezone.utc).isoformat(),
    }

    response = RedirectResponse(url="/vendor/register", status_code=303)
    if session:
        update_session_data(response, session, "registration_draft", draft)
    else:
        # Create a vendor session for non-logged-in users
        from app.session import create_session
        create_session(response, "vendor", email)
        # We need to set the draft in a fresh session - redirect will pick it up
        new_session = {
            "user_type": "vendor",
            "email": email,
            "created_at": __import__("time").time(),
            "last_activity": __import__("time").time(),
            "registration_draft": draft,
        }
        from app.session import _serializer, COOKIE_NAME
        from app.config import DEBUG
        signed = _serializer.dumps(new_session)
        response.set_cookie(
            key=COOKIE_NAME,
            value=signed,
            httponly=True,
            secure=not DEBUG,
            samesite="lax",
            path="/",
        )

    return response


# --- Step 2: Contact & Profile ---

@router.post("/register/step2")
async def register_step2(
    request: Request,
    business_name: str = Form(...),
    contact_name: str = Form(...),
    phone: str = Form(...),
    category: str = Form(...),
    description: str = Form(...),
    cuisine_type: str = Form(""),
    needs_power: str = Form(""),
    needs_water: str = Form(""),
    needs_propane: str = Form(""),
    _csrf: None = Depends(require_csrf),
):
    session = read_session(request)
    if not session:
        return RedirectResponse(url="/vendor/register", status_code=303)

    draft = session.get("registration_draft", {})
    errors = []

    if not business_name.strip():
        errors.append("Business name is required.")
    if not contact_name.strip():
        errors.append("Contact name is required.")
    if not phone.strip():
        errors.append("Phone number is required.")
    if category not in ("food", "non_food"):
        errors.append("Please select a valid category.")
    if not description.strip():
        errors.append("Description is required.")
    if category == "food" and not cuisine_type.strip():
        errors.append("Cuisine type is required for food vendors.")

    if errors:
        flash = [{"category": "error", "text": e} for e in errors]
        draft.update({
            "business_name": business_name,
            "contact_name": contact_name,
            "phone": phone,
            "category": category,
            "description": description,
            "cuisine_type": cuisine_type,
            "needs_power": needs_power,
            "needs_water": needs_water,
            "needs_propane": needs_propane,
        })
        return _template(request, "vendor/register_step2.html", {
            "draft": draft,
            "get_flashed_messages": lambda: flash,
        })

    draft.update({
        "current_step": 3,
        "business_name": business_name.strip(),
        "contact_name": contact_name.strip(),
        "phone": phone.strip(),
        "category": category,
        "description": description.strip(),
        "cuisine_type": cuisine_type.strip() if category == "food" else "",
        "needs_power": needs_power == "on",
        "needs_water": needs_water == "on",
        "needs_propane": needs_propane == "on",
    })

    response = RedirectResponse(url="/vendor/register", status_code=303)
    update_session_data(response, session, "registration_draft", draft)
    return response


# --- Step 3: Booth selection ---

@router.post("/register/step3")
async def register_step3(
    request: Request,
    booth_type_id: int = Form(...),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    session = read_session(request)
    if not session:
        return RedirectResponse(url="/vendor/register", status_code=303)

    draft = session.get("registration_draft", {})

    booth_type = db.query(BoothType).filter(BoothType.id == booth_type_id, BoothType.is_active == True).first()
    if not booth_type:
        flash = [{"category": "error", "text": "Please select a valid booth type."}]
        booth_types = db.query(BoothType).filter(BoothType.is_active == True).order_by(BoothType.sort_order).all()
        return _template(request, "vendor/register_step3.html", {
            "booth_types": booth_types,
            "draft": draft,
            "get_flashed_messages": lambda: flash,
        })

    draft.update({
        "current_step": 4,
        "booth_type_id": booth_type.id,
        "booth_type_name": booth_type.name,
        "booth_type_price": booth_type.price,
    })

    response = RedirectResponse(url="/vendor/register", status_code=303)
    update_session_data(response, session, "registration_draft", draft)
    return response


# --- Step 4: Final submit ---

@router.post("/register/step4")
async def register_step4(
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
        return _template(request, "vendor/register_step4.html", {
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
        "needs_power": draft.get("needs_power", False),
        "needs_water": draft.get("needs_water", False),
        "needs_propane": draft.get("needs_propane", False),
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

    return _template(request, "vendor/dashboard.html", {
        "registrations": reg_data,
    })
