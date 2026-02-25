# Asian Night Market — Vendor Registration

Vendor registration system for the Asian Night Market (Memphis, TN), replacing Google Forms with integrated payment and status tracking. Built for ~150 vendors with a three-phase workflow: **Register → Admin Approval → Payment**.

Organized by the Vietnamese American Community of West Tennessee.

## Features

- Multi-step registration with vendor agreement, profile, and booth selection
- Passwordless OTP authentication for vendors and admins
- Admin dashboard with registration management, inventory tracking, and capacity alerts
- Stripe integration for booth payments and refunds
- Insurance document upload and admin approval workflow
- Email notifications at each workflow stage (confirmation, approval, payment, refund)
- Configurable booth types, pricing, and event dates via seed file
- CSV export of registration data

## Architecture

| Component | Technology |
|-----------|-----------|
| Backend | Python / FastAPI |
| Frontend | Jinja2 server-rendered templates + vanilla JS |
| CSS | PicoCSS (classless) |
| Database | SQLite (WAL mode, local) / PostgreSQL (production) via SQLAlchemy ORM |
| Payments | Stripe (PaymentIntents API + Stripe.js / Elements) |
| Email | Resend |
| Auth | Custom passwordless OTP |

The registration state machine enforces these transitions:

```
Pending → Approved        (admin approves)
Pending → Rejected        (admin rejects)
Approved → Paid           (vendor pays via Stripe)
Approved → Rejected       (admin revokes before payment)
Paid → Cancelled          (admin cancels + Stripe refund)
```

Inventory is derived, not stored: available booths = `total_quantity - COUNT(status IN ('approved', 'paid'))`. No counter columns.

```
app/
  main.py           — FastAPI entry, startup events
  config.py         — env var loading
  database.py       — SQLAlchemy engine/session
  models.py         — all ORM models
  seed.py           — seed booth types and event settings from config/event.json
  routes/            — vendor.py, admin.py, auth.py, webhooks.py
  services/          — payment.py, email.py, registration.py
  templates/         — Jinja2 (vendor/, admin/, auth/, emails/)
  static/            — CSS overrides, JS
config/event.json   — booth types, event settings (seed data)
tests/              — pytest test suite
```

## Local Development Setup

### Prerequisites

Python 3.11+, a Stripe account (test mode), and a Resend account.

### 1. Clone and create a virtual environment

```bash
git clone https://github.com/vtphan/night-market-vendors.git
cd night-market-vendors
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your values:

| Variable | Required | Description |
|----------|----------|-------------|
| `SECRET_KEY` | Yes | Random string for session signing. Generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `STRIPE_PUBLISHABLE_KEY` | Yes | Stripe test-mode publishable key (`pk_test_...`) |
| `STRIPE_SECRET_KEY` | Yes | Stripe test-mode secret key (`sk_test_...`) |
| `STRIPE_WEBHOOK_SECRET` | Yes | Webhook signing secret from `stripe listen` (see below) |
| `RESEND_API_KEY` | Yes | Resend API key (`re_...`) |
| `EMAIL_FROM` | Yes | Sender address for outbound emails (e.g., `Asian Night Market <noreply@yourdomain.com>`) |
| `APP_URL` | Yes | Base URL of the app (e.g., `http://localhost:8000`) |
| `ADMIN_EMAILS` | Yes | Comma-separated list of email addresses that have admin access |
| `DATABASE_URL` | No | Defaults to `sqlite:///data/app.db`. Set to a PostgreSQL URL for production |
| `DEBUG` | No | Set to `true` for development (shows detailed errors) |

### 3. Configure event settings and booth types

Edit `config/event.json` to set the event name, dates, booth types, quantities, and pricing. This file is loaded on first startup to seed the database.

### 4. Install and run the Stripe CLI

The Stripe CLI forwards webhook events to your local server during development.

```bash
# Install (macOS)
brew install stripe/stripe-cli/stripe

# Login to your Stripe account
stripe login

# Forward webhooks to your local server
stripe listen --forward-to 127.0.0.1:8000/api/webhooks/stripe
```

Copy the webhook signing secret (`whsec_...`) printed by `stripe listen` into your `.env` file as `STRIPE_WEBHOOK_SECRET`.

### 5. Start the dev server

```bash
uvicorn app.main:app --reload --port 8000
```

The app auto-creates the SQLite database and seeds it from `config/event.json` on first startup.

### Admin access

The first admin is bootstrapped from the `ADMIN_EMAILS` environment variable. Log in at `/auth/login` with one of those email addresses — the OTP flow is the same as for vendors. Admin pages are at `/admin`.

### Schema changes during development

There are no migrations (no Alembic). To apply schema changes, delete the database and restart:

```bash
rm data/app.db
uvicorn app.main:app --reload --port 8000
```

The app will re-run `create_all()` and re-seed from `config/event.json`.

## Testing

```bash
# Run all tests
pytest

# Run a single test file
pytest tests/test_registration_transitions.py

# Run a single test with verbose output
pytest tests/test_auth.py::test_expired_otp_rejected -v
```

## Deployment

The app runs on a VPS (Hostinger) behind Nginx with Let's Encrypt SSL.

| Component | Detail |
|-----------|--------|
| Domain | `vendors.nightmarketmemphis.com` |
| OS | Ubuntu (Python 3.11) |
| Reverse proxy | Nginx with Let's Encrypt SSL |
| Process manager | systemd |

### Deploy updates

```bash
ssh vphan@82.25.86.134
cd /home/vphan/night-market-vendors
git pull
source venv/bin/activate
pip install -r requirements.txt   # only if dependencies changed
sudo systemctl restart vendor-registration
```

See `docs/deployment.md` for full server setup details (Nginx config, systemd unit, SSL).

## Documentation

Detailed documentation lives in the `docs/` directory:

| File | Contents |
|------|----------|
| `docs/spec.md` | Business requirements, workflows, rules |
| `docs/architecture.md` | Tech stack, database schema, integrations, security |
| `docs/deployment.md` | VPS setup, Nginx, systemd, SSL, deploy process |
| `docs/development_plan.md` | Phased build plan with test criteria |
| `docs/progress.md` | Changelog of decisions and what's been built |
