"""Generate pre-filled Shelby County food permit PDFs for food/beverage vendors."""

import logging
from pathlib import Path

from pypdf import PdfReader, PdfWriter

logger = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "food_permit.pdf"
PERMITS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "permits"

FOOD_CATEGORIES = {"food", "beverage"}


def generate_food_permit(
    registration_id: str,
    category: str,
    business_name: str,
    contact_name: str,
    address: str | None,
    city_state_zip: str | None,
    phone: str,
    email: str,
    description: str,
    event_name: str,
    event_location: str,
    event_dates: str,
    setup_time: str = "2:00 PM",
) -> Path | None:
    """Fill the food permit PDF template and save to data/permits/.

    Returns the path to the generated PDF, or None if the category
    doesn't require a food permit.
    """
    if category not in FOOD_CATEGORIES:
        return None

    PERMITS_DIR.mkdir(parents=True, exist_ok=True)

    reader = PdfReader(str(TEMPLATE_PATH))
    writer = PdfWriter()
    writer.append(reader)

    field_values = {
        "Event Name": event_name,
        "Location": event_location,
        "Dates": event_dates,
        "Setup Time": setup_time,
        "Company Name": business_name,
        "Contact Person": contact_name,
        "undefined": address or "",  # "undefined" is the Address field in the PDF
        "CityState and Zip": city_state_zip or "",
        "Contact Number": phone,
        "Email Address": email,
        "Check Box2": "/Yes",  # Eating/Drinking — always checked
    }

    # Split description across the 3 sampling/selling lines
    desc_lines = _split_description(description, max_lines=3)
    for i, line in enumerate(desc_lines, start=1):
        field_values[f"WHAT WILL YOU BE SAMPLINGSELLING {i}"] = line

    writer.update_page_form_field_values(writer.pages[0], field_values)

    output_path = PERMITS_DIR / f"{registration_id}.pdf"
    with open(output_path, "wb") as f:
        writer.write(f)

    logger.info("Generated food permit for %s at %s", registration_id, output_path)
    return output_path


def _split_description(description: str, max_lines: int = 3, max_chars_per_line: int = 80) -> list[str]:
    """Split a description into lines that fit the PDF form fields."""
    words = description.split()
    lines = []
    current_line = ""

    for word in words:
        if current_line and len(current_line) + 1 + len(word) > max_chars_per_line:
            lines.append(current_line)
            current_line = word
            if len(lines) >= max_lines:
                break
        else:
            current_line = f"{current_line} {word}".strip() if current_line else word

    if current_line and len(lines) < max_lines:
        lines.append(current_line)

    return lines
