from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, Date, ForeignKey,
)
from sqlalchemy.sql import func

from app.database import Base


class Registration(Base):
    __tablename__ = "registrations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    registration_id = Column(String(20), unique=True, nullable=False, index=True)
    email = Column(String, nullable=False, index=True)
    business_name = Column(String, nullable=False)
    contact_name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    category = Column(String(30), nullable=False)
    description = Column(Text, nullable=False)
    cuisine_type = Column(String, nullable=True)
    electrical_equipment = Column(String, nullable=True)
    electrical_other = Column(Text, nullable=True)
    booth_type_id = Column(Integer, ForeignKey("booth_types.id"), nullable=False)
    status = Column(String(50), nullable=False, default="pending", index=True)
    documents_approved = Column(Boolean, default=False)
    stripe_payment_intent_id = Column(String, nullable=True)
    amount_paid = Column(Integer, nullable=True)
    refund_amount = Column(Integer, default=0)
    approved_at = Column(DateTime, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    rejection_reason = Column(String, nullable=True)
    agreement_accepted_at = Column(DateTime, nullable=False)
    agreement_ip_address = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


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


class EventSettings(Base):
    __tablename__ = "event_settings"

    id = Column(Integer, primary_key=True, default=1)
    event_name = Column(String, nullable=False)
    event_date = Column(Date, nullable=False)
    registration_open_date = Column(DateTime, nullable=False)
    registration_close_date = Column(DateTime, nullable=False)
    vendor_agreement_text = Column(Text, nullable=False)
    front_page_content = Column(Text, nullable=False, default="")

    def is_registration_open(self) -> bool:
        """Check if vendor registration is currently open."""
        now = datetime.now(timezone.utc)
        open_dt = self.registration_open_date.replace(tzinfo=timezone.utc)
        close_dt = self.registration_close_date.replace(tzinfo=timezone.utc)
        return open_dt <= now <= close_dt
