import logging

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.csrf import generate_csrf_token, require_csrf
from app.session import create_session, clear_session, read_session
from app.services.otp import create_otp, validate_otp
from app.services.email import send_otp_email
from app.models import AdminUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    session = read_session(request)
    if session:
        if session.get("user_type") == "admin":
            return RedirectResponse(url="/admin", status_code=303)
        return RedirectResponse(url="/vendor/dashboard", status_code=303)

    return request.app.state.templates.TemplateResponse(
        "auth/login.html",
        {
            "request": request,
            "csrf_token": generate_csrf_token(),
            "session": None,
            "get_flashed_messages": lambda: request.app.state.flash.get(id(request), []),
        },
    )


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    email = email.lower().strip()
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
            "session": None,
            "get_flashed_messages": lambda: [],
        },
    )


@router.post("/verify")
async def verify_submit(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    email = email.lower().strip()

    if validate_otp(db, email, code):
        # Determine user type
        admin = (
            db.query(AdminUser)
            .filter(AdminUser.email == email, AdminUser.is_active == True)
            .first()
        )
        user_type = "admin" if admin else "vendor"

        redirect_url = "/admin" if user_type == "admin" else "/vendor/dashboard"
        response = RedirectResponse(url=redirect_url, status_code=303)
        create_session(response, user_type, email)
        logger.info("Login successful: %s (%s)", email, user_type)
        return response
    else:
        flash_messages = [{"category": "error", "text": "Invalid or expired code. Please try again."}]
        return request.app.state.templates.TemplateResponse(
            "auth/verify.html",
            {
                "request": request,
                "csrf_token": generate_csrf_token(),
                "email": email,
                "session": None,
                "get_flashed_messages": lambda: flash_messages,
            },
            status_code=400,
        )


@router.get("/verify", response_class=HTMLResponse)
async def verify_page(request: Request, email: str = ""):
    if not email:
        return RedirectResponse(url="/auth/login", status_code=303)
    return request.app.state.templates.TemplateResponse(
        "auth/verify.html",
        {
            "request": request,
            "csrf_token": generate_csrf_token(),
            "email": email,
            "session": None,
            "get_flashed_messages": lambda: [],
        },
    )


@router.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/auth/login", status_code=303)
    clear_session(response)
    return response
