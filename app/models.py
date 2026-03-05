from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint, Column, Integer, Float, String, Text, Boolean, DateTime, Date, ForeignKey,
)
from sqlalchemy.sql import func

from app.database import Base


class Registration(Base):
    __tablename__ = "registrations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'paid', 'cancelled', 'withdrawn')",
            name="ck_registration_status",
        ),
        CheckConstraint("amount_paid IS NULL OR amount_paid >= 0", name="ck_amount_paid_non_negative"),
        CheckConstraint("processing_fee IS NULL OR processing_fee >= 0", name="ck_processing_fee_non_negative"),
        CheckConstraint("refund_amount >= 0", name="ck_refund_amount_non_negative"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    registration_id = Column(String(20), unique=True, nullable=False, index=True)
    email = Column(String, nullable=False, index=True)
    business_name = Column(String, nullable=False)
    contact_name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    category = Column(String(30), nullable=False)
    description = Column(Text, nullable=False)
    electrical_equipment = Column(String, nullable=True)
    electrical_other = Column(Text, nullable=True)
    booth_type_id = Column(Integer, ForeignKey("booth_types.id"), nullable=False)
    status = Column(String(50), nullable=False, default="pending", index=True)
    stripe_payment_intent_id = Column(String, nullable=True)
    approved_price = Column(Integer, nullable=True)
    amount_paid = Column(Integer, nullable=True)
    processing_fee = Column(Integer, nullable=True)
    refund_amount = Column(Integer, nullable=False, default=0)
    approved_at = Column(DateTime, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    withdrawn_at = Column(DateTime, nullable=True)
    reversal_reason = Column(String, nullable=True)
    agreement_accepted_at = Column(DateTime, nullable=False)
    agreement_ip_address = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    admin_notes = Column(Text, nullable=True)  # legacy — migrated to admin_notes table
    concern_status = Column(String(10), nullable=False, default="none", server_default="none")
    payment_deadline = Column(DateTime, nullable=True)
    last_reminder_sent_at = Column(DateTime, nullable=True)
    reminder_count = Column(Integer, default=0, server_default="0")
    last_insurance_reminder_sent_at = Column(DateTime, nullable=True)
    insurance_reminder_count = Column(Integer, default=0, server_default="0")
    updated_at = Column(
        DateTime, nullable=False,
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class BoothType(Base):
    __tablename__ = "booth_types"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    description = Column(Text)
    total_quantity = Column(Integer, nullable=False)
    price = Column(Integer, nullable=False)  # in cents
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class OTPCode(Base):
    __tablename__ = "otp_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, nullable=False, index=True)
    code_hash = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, default=0)
    used = Column(Boolean, default=False)


class StripeEvent(Base):
    __tablename__ = "stripe_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    stripe_event_id = Column(String, unique=True, nullable=False)
    event_type = Column(String, nullable=False)
    processed_at = Column(DateTime, nullable=False, server_default=func.now())


class InsuranceDocument(Base):
    __tablename__ = "insurance_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False, index=True)
    original_filename = Column(String, nullable=False)
    stored_filename = Column(String, unique=True, nullable=False)
    content_type = Column(String, nullable=False)
    file_size = Column(Integer, nullable=False)
    is_approved = Column(Boolean, default=False)
    approved_by = Column(String, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    uploaded_at = Column(DateTime, nullable=False, server_default=func.now())


class AdminNote(Base):
    __tablename__ = "admin_notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    registration_id = Column(String(20), ForeignKey("registrations.registration_id"), nullable=False, index=True)
    admin_email = Column(String, nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class RegistrationDraft(Base):
    __tablename__ = "registration_drafts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False, index=True)
    draft_json = Column(Text, nullable=False)
    updated_at = Column(
        DateTime, nullable=False,
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class EventSettings(Base):
    __tablename__ = "event_settings"

    id = Column(Integer, primary_key=True, default=1)
    event_name = Column(String, nullable=False)
    event_start_date = Column(Date, nullable=False)
    event_end_date = Column(Date, nullable=False)
    registration_open_date = Column(DateTime, nullable=False)
    registration_close_date = Column(DateTime, nullable=False)
    vendor_agreement_text = Column(Text, nullable=False)
    front_page_content = Column(Text, nullable=False, default="")
    banner_text = Column(Text, nullable=False, default="")
    contact_email = Column(String, nullable=False, default="")
    developer_contact = Column(String, nullable=False, default="")
    payment_instructions = Column(Text, nullable=False, default="")
    insurance_instructions = Column(Text, nullable=False, default="")
    processing_fee_percent = Column(Float, default=2.9)
    processing_fee_flat_cents = Column(Integer, default=30)
    refund_policy = Column(Text, default="")
    refund_presets = Column(String, default="100,75,50,25,0")
    notify_new_registration = Column(Boolean, default=False, server_default="0")
    notify_payment_received = Column(Boolean, default=False, server_default="0")
    notify_insurance_uploaded = Column(Boolean, default=False, server_default="0")
    payment_deadline_days = Column(Integer, default=7, server_default="7")
    reminder_1_days = Column(Integer, default=2, server_default="2")
    reminder_2_days = Column(Integer, default=5, server_default="5")
    reminder_1_subject = Column(String, default="Payment Reminder — {event_name}")
    reminder_1_body = Column(Text, default="")
    reminder_2_subject = Column(String, default="Urgent: Payment Deadline Approaching — {event_name}")
    reminder_2_body = Column(Text, default="")

    @staticmethod
    def derive_reminder_defaults(deadline_days: int) -> tuple[int, int]:
        """Compute default reminder days from deadline.

        R1 = roughly 1/3 of deadline, R2 = roughly 2/3 of deadline,
        each clamped to at least 1 day apart from boundaries.
        """
        if deadline_days <= 2:
            return (1, 1)
        if deadline_days <= 3:
            return (1, 2)
        r1 = max(1, deadline_days // 3)
        r2 = max(r1 + 1, (deadline_days * 2) // 3)
        r2 = min(r2, deadline_days - 1)
        return (r1, r2)

    def validate_reminder_days(self) -> list[str]:
        """Validate that 1 <= R1 < R2 < deadline with 1-day gaps."""
        errors = []
        d = self.payment_deadline_days
        r1 = self.reminder_1_days
        r2 = self.reminder_2_days
        if d is None or d < 1:
            errors.append("Payment deadline must be at least 1 day.")
            return errors
        if r1 < 1:
            errors.append("Reminder 1 must be at least 1 day after approval.")
        if r2 < 1:
            errors.append("Reminder 2 must be at least 1 day after approval.")
        if r1 >= r2:
            errors.append("Reminder 1 must be earlier than Reminder 2.")
        if r2 >= d:
            errors.append("Reminder 2 must be before the payment deadline.")
        if r1 >= 1 and r2 >= 1 and r2 - r1 < 1:
            errors.append("There must be at least 1 day between Reminder 1 and Reminder 2.")
        return errors

    @staticmethod
    def _ensure_utc(dt: datetime) -> datetime:
        """Ensure a datetime is UTC-aware (handles both naive and aware)."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def is_registration_open(self) -> bool:
        """Check if vendor registration is currently open."""
        now = datetime.now(timezone.utc)
        open_dt = self._ensure_utc(self.registration_open_date)
        close_dt = self._ensure_utc(self.registration_close_date)
        return open_dt <= now <= close_dt

    def get_registration_status(self) -> str:
        """Return registration window status: 'open', 'coming_soon', or 'closed'."""
        now = datetime.now(timezone.utc)
        open_dt = self._ensure_utc(self.registration_open_date)
        close_dt = self._ensure_utc(self.registration_close_date)
        if open_dt <= now <= close_dt:
            return "open"
        elif now < open_dt:
            return "coming_soon"
        else:
            return "closed"
