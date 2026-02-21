import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import DEBUG
from app.database import Base, engine, SessionLocal
from app.seed import seed_event_data, bootstrap_admins
from app.session import read_session, refresh_session

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

# Static files
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

# Session refresh middleware
@app.middleware("http")
async def session_refresh_middleware(request: Request, call_next):
    response = await call_next(request)
    session = read_session(request)
    if session:
        refresh_session(response, session)
    return response


# Health check
@app.get("/health")
async def health_check():
    return JSONResponse({"status": "ok"})


# Redirect root to login
@app.get("/")
async def root():
    return RedirectResponse(url="/auth/login", status_code=303)


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

app.include_router(auth_router)
app.include_router(admin_router)
