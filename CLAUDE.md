# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-event vendor registration app for the Asian Night Market (~150 vendors). Replaces Google Forms with integrated payment and status tracking. Three-phase workflow: Register → Admin Approval → Payment.

## Tech Stack

- **Backend:** Python / FastAPI
- **Frontend:** Jinja2 server-rendered templates + vanilla JS
- **CSS:** PicoCSS (classless — use semantic HTML, no utility classes)
- **Database:** SQLite (WAL mode, local dev) / Supabase PostgreSQL (production) via SQLAlchemy ORM
- **Payments:** Stripe (PaymentIntents API + Stripe.js / Elements)
- **Email:** Resend
- **Hosting:** Railway

## Commands

```bash
# Run dev server
uvicorn app.main:app --reload --port 8000

# Run all tests
pytest

# Run a single test file
pytest tests/test_registration_transitions.py

# Run a single test
pytest tests/test_auth.py::test_expired_otp_rejected -v

# Stripe webhook forwarding (local dev)
stripe listen --forward-to 127.0.0.1:8000/api/webhooks/stripe
```

## Architecture

### Registration Statuses (state machine)

```
Pending → Approved        (admin approves)
Pending → Rejected        (admin rejects)
Approved → Confirmed      (vendor pays via Stripe)
Approved → Rejected       (admin revokes before payment)
Confirmed → Cancelled     (admin cancels + Stripe refund)
```

All transitions enforced in `app/services/registration.py`. Any transition not in this list must be rejected.

### Database Tables

Six tables defined in `app/models.py`: `registrations`, `booth_types`, `admin_users`, `otp_codes`, `stripe_events`, `event_settings`. Schema details in `docs/architecture.md` §3.

One registration = one vendor + one booth. No separate vendors/orders tables.

### Key Design Decisions

- **Approval-first workflow:** Admin must approve before vendor can pay. No inventory race conditions — admin decides based on dashboard availability counts.
- **Inventory is derived, not stored:** Available booths = `total_quantity - COUNT(status IN ('approved', 'confirmed'))`. No counter columns.
- **OTP auth for everyone:** Vendors and admins use the same passwordless OTP flow. Admin access determined by `admin_users` table (bootstrapped from `ADMIN_EMAILS` env var).
- **`documents_approved` is informational only:** Does not affect registration status or block payment.
- **Schema changes during dev:** Delete `data/app.db` and restart (runs `create_all()` + seed). No Alembic.

### Project Structure

```
app/
  main.py           — FastAPI entry, startup events
  config.py         — env var loading
  database.py       — SQLAlchemy engine/session (SQLite or Postgres based on DATABASE_URL)
  models.py         — all ORM models
  seed.py           — seed booth_types and event_settings from config/event.json
  routes/            — vendor.py, admin.py, auth.py, webhooks.py
  services/          — payment.py, email.py, registration.py
  templates/         — Jinja2 (vendor/, admin/, auth/, emails/)
  static/            — CSS overrides, JS
config/event.json   — booth types, event settings (seed data)
```

### Stripe Integration

- PaymentIntent created server-side only for Approved registrations
- Card input via Stripe Elements (PCI-compliant; card data never touches our server)
- `payment_intent.succeeded` webhook transitions Approved → Confirmed
- Refunds via `stripe.Refund.create()` on admin cancellation
- Webhook idempotency enforced via `stripe_events` table

### Email

All emails via Resend. On send failure: log and continue (never block user flow) — **except** OTP delivery, which shows a retry message. Email triggers: submission confirmation, approval notification (with payment link), rejection, payment confirmation, refund confirmation.

## Documentation

- `docs/spec.md` — business requirements, workflows, rules (the "what")
- `docs/architecture.md` — tech stack, schema, integrations, security (the "how")
- `docs/development_plan.md` — phased build plan with test criteria (the "when")
- `docs/progress.md` — changelog of decisions and what's been built

## Environment Variables

See `.env.example`. Key vars: `STRIPE_PUBLISHABLE_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `RESEND_API_KEY`, `EMAIL_FROM`, `SECRET_KEY`, `APP_URL`, `DATABASE_URL`, `ADMIN_EMAILS`, `DEBUG`.
