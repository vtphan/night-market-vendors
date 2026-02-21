import logging
from pathlib import Path

import resend
from jinja2 import Environment, FileSystemLoader

from app.config import RESEND_API_KEY, EMAIL_FROM

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
