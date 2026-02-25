import logging
import secrets
from urllib.parse import urlencode

import httpx
from authlib.jose import jwt as jose_jwt
from authlib.jose.errors import JoseError
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy.orm import Session

from app.config import (
    APP_URL, SECRET_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_OAUTH_ENABLED,
)
from app.database import get_db
from app.csrf import generate_csrf_token, require_csrf
from app.session import create_session, clear_session, read_session
from app.services.otp import create_otp, validate_otp
from app.services.email import send_otp_email
from app.models import AdminUser, EventSettings

logger = logging.getLogger(__name__)

_state_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="oauth-state")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

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
            "google_oauth_enabled": GOOGLE_OAUTH_ENABLED,
            "get_flashed_messages": lambda: [],
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

    # Block non-admin emails early — don't waste an OTP
    if role == "admin":
        admin = (
            db.query(AdminUser)
            .filter(AdminUser.email == email, AdminUser.is_active == True)
            .first()
        )
        if not admin:
            flash_messages.append({"category": "error", "text": "This email is not authorized for admin access."})
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
                status_code=403,
            )

    code = create_otp(db, email)
    if code is None:
        flash_messages.append({"category": "error", "text": "Too many attempts. Please wait before trying again."})
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


@router.get("/google")
async def google_login(request: Request, role: str = "vendor", db: Session = Depends(get_db)):
    if not GOOGLE_OAUTH_ENABLED:
        return RedirectResponse(url="/auth/login", status_code=303)

    role = role if role == "admin" else "vendor"

    # Block vendor OAuth when registration is closed
    if role == "vendor" and not _is_registration_open(db):
        return RedirectResponse(url="/", status_code=303)

    # Create signed state containing the role and a nonce
    nonce = secrets.token_urlsafe(16)
    state_data = {"role": role, "nonce": nonce}
    state = _state_serializer.dumps(state_data)

    # Build Google authorization URL directly (no SessionMiddleware needed)
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": f"{APP_URL}/auth/google/callback",
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "nonce": nonce,
    }
    google_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    response = RedirectResponse(url=google_url, status_code=302)

    # Set state cookie for CSRF verification in callback
    response.set_cookie(
        key="oauth_state",
        value=state,
        max_age=300,  # 5 minutes
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/google/callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    if not GOOGLE_OAUTH_ENABLED:
        return RedirectResponse(url="/auth/login", status_code=303)

    # Check for error from Google
    error = request.query_params.get("error")
    if error:
        logger.warning("Google OAuth error: %s", error)
        return RedirectResponse(url="/auth/login?role=vendor", status_code=303)

    # Validate state cookie
    state_cookie = request.cookies.get("oauth_state")
    state_param = request.query_params.get("state")
    if not state_cookie or not state_param or state_cookie != state_param:
        logger.warning("OAuth state mismatch")
        return RedirectResponse(url="/auth/login", status_code=303)

    # Verify state signature and expiry (5 min max)
    try:
        state_data = _state_serializer.loads(state_cookie, max_age=300)
    except (BadSignature, SignatureExpired):
        logger.warning("OAuth state invalid or expired")
        return RedirectResponse(url="/auth/login", status_code=303)

    role = state_data.get("role", "vendor")
    role = role if role == "admin" else "vendor"

    # Exchange code for tokens via Google's token endpoint
    auth_code = request.query_params.get("code")
    if not auth_code:
        logger.warning("No authorization code in Google callback")
        return RedirectResponse(url="/auth/login", status_code=303)

    try:
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(GOOGLE_TOKEN_URL, data={
                "code": auth_code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": f"{APP_URL}/auth/google/callback",
                "grant_type": "authorization_code",
            })
            token_data = token_resp.json()

            if "error" in token_data:
                logger.warning("Google token exchange error: %s", token_data.get("error"))
                return RedirectResponse(url="/auth/login", status_code=303)

            # Fetch Google's public keys and verify the ID token
            jwks_resp = await client.get(GOOGLE_JWKS_URL)
            jwks = jwks_resp.json()

        id_token = token_data.get("id_token")
        if not id_token:
            logger.warning("No id_token in Google token response")
            return RedirectResponse(url="/auth/login", status_code=303)

        claims = jose_jwt.decode(id_token, jwks)
        claims.validate()
        email = claims.get("email", "").lower().strip()
    except (JoseError, httpx.HTTPError, Exception):
        logger.exception("Failed to exchange/verify Google OAuth token")
        return RedirectResponse(url="/auth/login", status_code=303)

    if not email:
        logger.warning("No email in Google ID token")
        return RedirectResponse(url="/auth/login", status_code=303)

    # Role-based validation
    if role == "admin":
        admin = (
            db.query(AdminUser)
            .filter(AdminUser.email == email, AdminUser.is_active == True)
            .first()
        )
        if not admin:
            flash_messages = [{"category": "error", "text": "This email is not authorized for admin access."}]
            return request.app.state.templates.TemplateResponse(
                "auth/login.html",
                {
                    "request": request,
                    "csrf_token": generate_csrf_token(),
                    "session": None,
                    "role": role,
                    "registration_open": _is_registration_open(db),
                    "google_oauth_enabled": GOOGLE_OAUTH_ENABLED,
                    "get_flashed_messages": lambda: flash_messages,
                },
                status_code=403,
            )
    else:
        # Vendor: check registration is open
        if not _is_registration_open(db):
            return RedirectResponse(url="/", status_code=303)

    # Create session (same as OTP flow)
    redirect_url = "/admin" if role == "admin" else "/vendor/dashboard"
    response = RedirectResponse(url=redirect_url, status_code=303)
    create_session(response, role, email)
    response.delete_cookie(key="oauth_state", path="/")
    logger.info("Google OAuth login successful: %s (%s)", email, role)
    return response


@router.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/", status_code=303)
    clear_session(response)
    return response
