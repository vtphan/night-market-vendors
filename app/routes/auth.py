import logging

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.csrf import generate_csrf_token, require_csrf
from app.session import create_session, clear_session, read_session
from app.services.otp import create_otp, validate_otp
from app.services.email import send_otp_email
from app.models import AdminUser, EventSettings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _is_registration_open(db: Session) -> bool:
    """Check if vendor registration is currently open."""
    settings = db.query(EventSettings).first()
    return settings.is_registration_open() if settings else False


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, role: str = "vendor", db: Session = Depends(get_db)):
    role = role if role == "admin" else "vendor"
    registration_open = _is_registration_open(db)

    session = read_session(request)
    if session:
        if session.get("user_type") == "admin":
            return RedirectResponse(url="/admin", status_code=303)
        return RedirectResponse(url="/vendor/dashboard", status_code=303)

    # If registration is closed and a vendor tries to access, send to homepage
    if not registration_open and role == "vendor":
        return RedirectResponse(url="/", status_code=303)

    return request.app.state.templates.TemplateResponse(
        "auth/login.html",
        {
            "request": request,
            "csrf_token": generate_csrf_token(),
            "session": None,
            "role": role,
            "registration_open": registration_open,
            "get_flashed_messages": lambda: request.app.state.flash.get(id(request), []),
        },
    )


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    role: str = Form("vendor"),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    email = email.lower().strip()
    role = role if role == "admin" else "vendor"
    registration_open = _is_registration_open(db)
    flash_messages = []

    code = create_otp(db, email)
    if code is None:
        flash_messages.append({"category": "error", "text": "Too many attempts. Please wait before trying again."})
        request.app.state.flash[id(request)] = flash_messages
        return request.app.state.templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "csrf_token": generate_csrf_token(),
                "session": None,
                "role": role,
                "registration_open": registration_open,
                "get_flashed_messages": lambda: flash_messages,
            },
            status_code=429,
        )

    success = send_otp_email(email, code)
    if not success:
        flash_messages.append({"category": "error", "text": "We couldn't send the verification code. Please try again."})
        request.app.state.flash[id(request)] = flash_messages
        return request.app.state.templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "csrf_token": generate_csrf_token(),
                "session": None,
                "role": role,
                "registration_open": registration_open,
                "get_flashed_messages": lambda: flash_messages,
            },
            status_code=500,
        )

    logger.info("OTP sent to %s", email)
    return request.app.state.templates.TemplateResponse(
        "auth/verify.html",
        {
            "request": request,
            "csrf_token": generate_csrf_token(),
            "email": email,
            "role": role,
            "session": None,
            "get_flashed_messages": lambda: [],
        },
    )


@router.post("/verify")
async def verify_submit(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    role: str = Form("vendor"),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    email = email.lower().strip()
    role = role if role == "admin" else "vendor"

    if validate_otp(db, email, code):
        # If role=admin, verify the email is actually in admin_users
        if role == "admin":
            admin = (
                db.query(AdminUser)
                .filter(AdminUser.email == email, AdminUser.is_active == True)
                .first()
            )
            if not admin:
                flash_messages = [{"category": "error", "text": "This email is not authorized for admin access."}]
                return request.app.state.templates.TemplateResponse(
                    "auth/verify.html",
                    {
                        "request": request,
                        "csrf_token": generate_csrf_token(),
                        "email": email,
                        "role": role,
                        "session": None,
                        "get_flashed_messages": lambda: flash_messages,
                    },
                    status_code=403,
                )

        redirect_url = "/admin" if role == "admin" else "/vendor/dashboard"
        response = RedirectResponse(url=redirect_url, status_code=303)
        create_session(response, role, email)
        logger.info("Login successful: %s (%s)", email, role)
        return response
    else:
        flash_messages = [{"category": "error", "text": "Invalid or expired code. Please try again."}]
        return request.app.state.templates.TemplateResponse(
            "auth/verify.html",
            {
                "request": request,
                "csrf_token": generate_csrf_token(),
                "email": email,
                "role": role,
                "session": None,
                "get_flashed_messages": lambda: flash_messages,
            },
            status_code=400,
        )


@router.get("/verify", response_class=HTMLResponse)
async def verify_page(request: Request, email: str = "", role: str = "vendor"):
    if not email:
        return RedirectResponse(url="/auth/login", status_code=303)
    role = role if role == "admin" else "vendor"
    return request.app.state.templates.TemplateResponse(
        "auth/verify.html",
        {
            "request": request,
            "csrf_token": generate_csrf_token(),
            "email": email,
            "role": role,
            "session": None,
            "get_flashed_messages": lambda: [],
        },
    )


@router.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/", status_code=303)
    clear_session(response)
    return response
