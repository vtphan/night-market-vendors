import logging
from pathlib import Path

import resend
from jinja2 import Environment, FileSystemLoader

from app.config import RESEND_API_KEY, EMAIL_FROM, APP_URL, ADMIN_EMAILS

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


def send_approval_email(to: str, registration_id: str, payment_url: str, insurance_instructions: str = "") -> bool:
    """Send registration approval notification with payment link."""
    try:
        template = _env.get_template("approval.html")
        html = template.render(
            registration_id=registration_id,
            payment_url=payment_url,
            insurance_instructions=insurance_instructions,
        )
    except Exception:
        logger.exception("Failed to render approval email template")
        return False
    return send_email(to, f"Registration {registration_id} Approved!", html)


def send_payment_confirmation_email(to: str, registration_id: str, booth_type_name: str, amount_cents: int) -> bool:
    """Send payment confirmation email."""
    try:
        template = _env.get_template("payment_confirmation.html")
        html = template.render(
            registration_id=registration_id,
            booth_type_name=booth_type_name,
            amount=f"${amount_cents / 100:.2f}",
            dashboard_url=f"{APP_URL}/vendor/dashboard",
        )
    except Exception:
        logger.exception("Failed to render payment confirmation template")
        return False
    return send_email(to, f"Payment Confirmed - {registration_id}", html)


def send_refund_email(
    to: str,
    registration_id: str,
    refund_amount_cents: int,
    reason: str | None = None,
    processing_fee_cents: int = 0,
) -> bool:
    """Send refund notification email."""
    try:
        template = _env.get_template("refund_confirmation.html")
        html = template.render(
            registration_id=registration_id,
            refund_amount=f"${refund_amount_cents / 100:.2f}",
            reason=reason,
            processing_fee=f"${processing_fee_cents / 100:.2f}" if processing_fee_cents else None,
        )
    except Exception:
        logger.exception("Failed to render refund confirmation template")
        return False
    return send_email(to, f"Refund Issued - {registration_id}", html)


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


def send_admin_notification_email(
    event_type: str,
    registration_id: str,
    business_name: str,
    detail_url: str,
) -> None:
    """Send a notification email to all admin emails."""
    if not ADMIN_EMAILS:
        return
    try:
        template = _env.get_template("admin_notification.html")
        html = template.render(
            event_type=event_type,
            registration_id=registration_id,
            business_name=business_name,
            detail_url=detail_url,
        )
    except Exception:
        logger.exception("Failed to render admin notification template")
        return

    subject_map = {
        "new_registration": f"Night Market: New Registration {registration_id}",
        "payment_received": f"Night Market: Payment Received {registration_id}",
        "insurance_uploaded": f"Night Market: Insurance Uploaded {registration_id}",
    }
    subject = subject_map.get(event_type, f"Night Market: {registration_id}")

    for admin_email in ADMIN_EMAILS:
        send_email(admin_email, subject, html)


def send_admin_alert_email(subject: str, body_text: str) -> None:
    """Send an urgent alert email to all admins (plain text, no template)."""
    if not ADMIN_EMAILS:
        return
    html = f"<pre>{body_text}</pre>"
    for admin_email in ADMIN_EMAILS:
        send_email(admin_email, subject, html)
