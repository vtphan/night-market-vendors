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
from app.models import EventSettings, BoothType

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
        settings = db.query(EventSettings).first()
        app.state.event_name = settings.event_name if settings else "Vendor Registration"
    finally:
        db.close()

    logger.info("Application startup complete")
    yield
    # Shutdown
    logger.info("Application shutting down")


app = FastAPI(title="Asian Night Market - Vendor Registration", lifespan=lifespan)

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


from app.services.registration import CATEGORIES, ELECTRICAL_EQUIPMENT_OPTIONS, EQUIP_LABELS

def get_event_name():
    return getattr(app.state, "event_name", "Vendor Registration")

app.state.templates.env.globals["get_event_name"] = get_event_name
app.state.templates.env.globals["format_price"] = format_price
app.state.templates.env.globals["format_datetime"] = format_datetime
app.state.templates.env.globals["CATEGORIES"] = CATEGORIES
app.state.templates.env.globals["ELECTRICAL_EQUIPMENT_OPTIONS"] = ELECTRICAL_EQUIPMENT_OPTIONS
app.state.templates.env.globals["EQUIP_LABELS"] = EQUIP_LABELS

# Static files
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

# Session refresh middleware (also catches unhandled exceptions for friendly 500s)
@app.middleware("http")
async def session_refresh_middleware(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return _error_response(request, 500, "Something Went Wrong",
                               "An unexpected error occurred. Please try again later.")

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

    session = read_session(request)
    settings = db.query(EventSettings).first()

    if not settings:
        return RedirectResponse(url="/auth/login", status_code=303)

    if settings.is_registration_open():
        status = "open"
    elif datetime.now(timezone.utc) < settings.registration_open_date.replace(tzinfo=timezone.utc):
        status = "coming_soon"
    else:
        status = "closed"

    registration_open = (status == "open")

    booth_types = (
        db.query(BoothType)
        .filter(BoothType.is_active == True)
        .order_by(BoothType.sort_order)
        .all()
    )

    return app.state.templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "settings": settings,
            "status": status,
            "session": session,
            "registration_open": registration_open,
            "booth_types": booth_types,
            "get_flashed_messages": lambda: [],
        },
    )


# Custom exception handlers
from starlette.exceptions import HTTPException as StarletteHTTPException

_ERROR_TITLES = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Page Not Found",
    405: "Method Not Allowed",
    429: "Too Many Requests",
}

_ERROR_MESSAGES = {
    400: "The request could not be understood. Please check your input and try again.",
    403: "You don't have permission to access this page.",
    404: "The page you're looking for doesn't exist or has been moved.",
    405: "This action is not supported.",
    429: "You've made too many requests. Please wait a moment and try again.",
}


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def _error_response(request: Request, status_code: int, title: str, message: str):
    """Build an HTML or JSON error response based on Accept header."""
    if _wants_html(request):
        return app.state.templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "session": read_session(request),
                "status_code": status_code,
                "title": title,
                "message": message,
                "get_flashed_messages": lambda: [],
            },
            status_code=status_code,
        )
    return JSONResponse(
        status_code=status_code,
        content={"detail": message},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 303 and "Location" in (exc.headers or {}):
        return RedirectResponse(url=exc.headers["Location"], status_code=303)

    title = _ERROR_TITLES.get(exc.status_code, "Error")
    message = exc.detail if isinstance(exc.detail, str) else _ERROR_MESSAGES.get(exc.status_code, "An unexpected error occurred.")
    return _error_response(request, exc.status_code, title, message)


# Include routers
from app.routes.auth import router as auth_router
from app.routes.admin import router as admin_router
from app.routes.vendor import router as vendor_router
from app.routes.webhooks import router as webhooks_router

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(vendor_router)
app.include_router(webhooks_router)
