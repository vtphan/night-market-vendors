import logging
from pathlib import Path

import resend
from jinja2 import Environment, FileSystemLoader

from app.config import RESEND_API_KEY, EMAIL_FROM, APP_URL

logger = logging.getLogger(__name__)

resend.api_key = RESEND_API_KEY

# Email template environment (separate from web templates)
_template_dir = Path(__file__).resolve().parent.parent / "templates" / "emails"
_env = Environment(loader=FileSystemLoader(str(_template_dir)), autoescape=True)


def send_email(to: str, subject: str, html_body: str) -> bool:
    """Send an email via Resend. Returns True on success, False on failure."""
    try:
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": [to],
            "subject": subject,
            "html": html_body,
        })
        logger.info("Email sent to %s: %s", to, subject)
        return True
    except Exception:
        logger.exception("Failed to send email to %s: %s", to, subject)
        return False


def send_otp_email(to: str, code: str) -> bool:
    """Send OTP verification email. Returns True on success."""
    try:
        template = _env.get_template("otp.html")
        html = template.render(code=code)
    except Exception:
        logger.exception("Failed to render OTP email template")
        return False

    return send_email(to, "Your verification code", html)


def send_submission_confirmation_email(to: str, registration_id: str, booth_type_name: str) -> bool:
    """Send registration submission confirmation email."""
    try:
        template = _env.get_template("submission_confirmation.html")
        html = template.render(
            registration_id=registration_id,
            booth_type_name=booth_type_name,
            dashboard_url=f"{APP_URL}/vendor/dashboard",
        )
    except Exception:
        logger.exception("Failed to render submission confirmation template")
        return False
    return send_email(to, f"Registration {registration_id} Received", html)


def send_approval_email(to: str, registration_id: str, payment_url: str) -> bool:
    """Send registration approval notification with payment link."""
    try:
        template = _env.get_template("approval.html")
        html = template.render(
            registration_id=registration_id,
            payment_url=payment_url,
        )
    except Exception:
        logger.exception("Failed to render approval email template")
        return False
    return send_email(to, f"Registration {registration_id} Approved!", html)


def send_rejection_email(to: str, registration_id: str, reason: str | None = None) -> bool:
    """Send registration rejection notification."""
    try:
        template = _env.get_template("rejection.html")
        html = template.render(
            registration_id=registration_id,
            reason=reason,
        )
    except Exception:
        logger.exception("Failed to render rejection email template")
        return False
    return send_email(to, f"Registration {registration_id} Update", html)
