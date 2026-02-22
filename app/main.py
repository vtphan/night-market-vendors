import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import DEBUG
from app.database import Base, engine, SessionLocal, get_db
from app.seed import seed_event_data, bootstrap_admins
from app.session import read_session, refresh_session
from app.models import EventSettings

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Creating database tables...")
    Base.metadata.create_all(bind=engine)

    logger.info("Running seed data...")
    db = SessionLocal()
    try:
        seed_event_data(db)
        bootstrap_admins(db)
    finally:
        db.close()

    logger.info("Application startup complete")
    yield
    # Shutdown
    logger.info("Application shutting down")


app = FastAPI(title="Asian Night Market - Vendor Registration", lifespan=lifespan)

# Flash message storage (simple in-memory, keyed by request id)
app.state.flash = {}

# Templates
app.state.templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


# Custom Jinja2 filters
def format_price(cents: int) -> str:
    """Convert cents to dollar string: 15000 → '$150.00'"""
    if cents is None:
        return "$0.00"
    return f"${cents / 100:.2f}"


def format_datetime(dt) -> str:
    """Format datetime for display."""
    if dt is None:
        return ""
    return dt.strftime("%b %d, %Y %I:%M %p")


app.state.templates.env.globals["format_price"] = format_price
app.state.templates.env.globals["format_datetime"] = format_datetime

# Static files
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

# Session refresh middleware
@app.middleware("http")
async def session_refresh_middleware(request: Request, call_next):
    response = await call_next(request)
    # Skip refresh if the route already set/deleted the session cookie
    # (e.g. login, logout, registration steps that update the draft).
    # Refreshing would overwrite the new cookie with stale data.
    already_set = any(
        b"session=" in header_value
        for header_name, header_value in response.raw_headers
        if header_name == b"set-cookie"
    )
    if not already_set:
        session = read_session(request)
        if session:
            refresh_session(response, session)
    return response


# Health check
@app.get("/health")
async def health_check():
    return JSONResponse({"status": "ok"})


# Homepage
@app.get("/")
async def homepage(request: Request, db = Depends(get_db)):
    from datetime import datetime, timezone
    from sqlalchemy.orm import Session as SASession

    session = read_session(request)
    settings = db.query(EventSettings).first()

    if not settings:
        return RedirectResponse(url="/auth/login", status_code=303)

    now = datetime.now(timezone.utc)
    open_dt = settings.registration_open_date.replace(tzinfo=timezone.utc)
    close_dt = settings.registration_close_date.replace(tzinfo=timezone.utc)

    if now < open_dt:
        status = "coming_soon"
    elif now <= close_dt:
        status = "open"
    else:
        status = "closed"

    registration_open = (status == "open")

    return app.state.templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "settings": settings,
            "status": status,
            "session": session,
            "registration_open": registration_open,
            "get_flashed_messages": lambda: [],
        },
    )


# Custom exception handler for 303 redirects from require_admin
from fastapi.exceptions import HTTPException as FastAPIHTTPException

@app.exception_handler(FastAPIHTTPException)
async def http_exception_handler(request: Request, exc: FastAPIHTTPException):
    if exc.status_code == 303 and "Location" in (exc.headers or {}):
        return RedirectResponse(url=exc.headers["Location"], status_code=303)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


# Include routers
from app.routes.auth import router as auth_router
from app.routes.admin import router as admin_router
from app.routes.vendor import router as vendor_router
from app.routes.webhooks import router as webhooks_router

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(vendor_router)
app.include_router(webhooks_router)
