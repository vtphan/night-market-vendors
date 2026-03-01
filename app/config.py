import os
from dotenv import load_dotenv

load_dotenv()

STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "noreply@example.com")

SECRET_KEY = os.getenv("SECRET_KEY", "")
APP_URL = os.getenv("APP_URL", "http://localhost:8000")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/app.db")

ADMIN_EMAILS = [
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "").split(",")
    if e.strip()
]

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

if not SECRET_KEY and not DEBUG:
    raise RuntimeError("SECRET_KEY environment variable is required in production")
if not SECRET_KEY:
    SECRET_KEY = "dev-only-insecure-key"
elif not DEBUG and len(SECRET_KEY) < 32:
    raise RuntimeError("SECRET_KEY must be at least 32 characters in production")

if not DEBUG:
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is required in production")
    if not STRIPE_WEBHOOK_SECRET:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is required in production")
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY is required in production")

# Google OAuth (optional — OTP still works without these)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_OAUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
