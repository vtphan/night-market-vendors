"""Generate PDF invoices for paid vendor registrations."""

import logging
from datetime import datetime
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

logger = logging.getLogger(__name__)

INVOICES_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "invoices"


def generate_invoice(
    registration_id: str,
    business_name: str,
    contact_name: str,
    email: str,
    phone: str,
    booth_type_name: str,
    approved_price_cents: int,
    processing_fee_cents: int,
    amount_paid_cents: int,
    paid_at: datetime,
    org_name: str = "",
    org_address: str = "",
    org_tax_id: str = "",
    event_name: str = "",
    stripe_payment_intent_id: str = "",
) -> Path:
    """Generate an invoice PDF and return the file path."""
    INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    output_path = INVOICES_DIR / f"{registration_id}.pdf"

    c = canvas.Canvas(str(output_path), pagesize=letter)
    width, height = letter

    y = height - 0.75 * inch

    # --- Header ---
    c.setFont("Helvetica-Bold", 18)
    c.drawString(0.75 * inch, y, "INVOICE")
    y -= 0.35 * inch

    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(0.75 * inch, y, f"Registration {registration_id}")
    y -= 0.15 * inch
    c.drawString(0.75 * inch, y, f"Date: {paid_at.strftime('%B %d, %Y')}")
    c.setFillColorRGB(0, 0, 0)
    y -= 0.5 * inch

    # --- From / To columns ---
    left_x = 0.75 * inch
    right_x = 4.25 * inch
    col_y = y

    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(left_x, col_y, "FROM")
    c.drawString(right_x, col_y, "TO")
    c.setFillColorRGB(0, 0, 0)
    col_y -= 0.2 * inch

    c.setFont("Helvetica", 10)
    from_lines = [org_name] if org_name else []
    if org_address:
        from_lines.extend(org_address.split(", "))
    if org_tax_id:
        from_lines.append(f"Tax ID: {org_tax_id}")

    to_lines = [business_name, contact_name, email, phone]

    for line in from_lines:
        c.drawString(left_x, col_y, line)
        col_y -= 0.18 * inch

    col_y = y - 0.2 * inch
    for line in to_lines:
        c.drawString(right_x, col_y, line)
        col_y -= 0.18 * inch

    y = min(col_y, y - 0.2 * inch - 0.18 * inch * len(from_lines)) - 0.3 * inch

    # --- Line items table ---
    table_left = 0.75 * inch
    desc_x = table_left
    amt_x = width - 0.75 * inch

    # Header row
    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(desc_x, y, "DESCRIPTION")
    c.drawRightString(amt_x, y, "AMOUNT")
    c.setFillColorRGB(0, 0, 0)
    y -= 0.08 * inch
    c.setStrokeColorRGB(0.8, 0.8, 0.8)
    c.line(table_left, y, amt_x, y)
    y -= 0.25 * inch

    c.setFont("Helvetica", 10)

    # Booth fee
    c.drawString(desc_x, y, f"{booth_type_name} — {event_name}" if event_name else booth_type_name)
    c.drawRightString(amt_x, y, _fmt_dollars(approved_price_cents))
    y -= 0.25 * inch

    # Processing fee
    if processing_fee_cents:
        c.drawString(desc_x, y, "Processing fee")
        c.drawRightString(amt_x, y, _fmt_dollars(processing_fee_cents))
        y -= 0.25 * inch

    # Separator
    c.setStrokeColorRGB(0.8, 0.8, 0.8)
    c.line(table_left, y, amt_x, y)
    y -= 0.25 * inch

    # Total
    c.setFont("Helvetica-Bold", 11)
    c.drawString(desc_x, y, "Total Paid")
    c.drawRightString(amt_x, y, _fmt_dollars(amount_paid_cents))
    y -= 0.45 * inch

    # --- Payment info ---
    if stripe_payment_intent_id:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.5, 0.5, 0.5)
        c.drawString(desc_x, y, f"Payment reference: {stripe_payment_intent_id}")
        y -= 0.15 * inch
        c.drawString(desc_x, y, f"Payment date: {paid_at.strftime('%B %d, %Y at %I:%M %p %Z').strip()}")
        c.setFillColorRGB(0, 0, 0)

    c.save()
    logger.info("Generated invoice for %s at %s", registration_id, output_path)
    return output_path


def _fmt_dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"
