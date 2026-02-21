# Architecture & Technical Design

## Asian Night Market — Vendor Registration App

> This document defines **how** the app is built — tech stack, database schema, integration details, security, and error handling. For business requirements, see [spec.md](spec.md). For the build plan, see [development_plan.md](development_plan.md).

---

## 1. Tech Stack

| Component | Technology |
|-----------|-----------|
| **Backend** | Python / FastAPI |
| **Frontend** | Jinja2 templates + vanilla JS |
| **CSS** | PicoCSS (classless, ~13KB) |
| **Database** | SQLite (WAL mode) + SQLAlchemy ORM |
| **Payments** | Stripe (PaymentIntents API + Stripe.js) |
| **Email** | Resend |
| **Hosting** | Railway |
| **Authentication** | Custom OTP |

Everything runs on one server: SQLite database file, FastAPI serves pages. Python is the developer's primary language. Server-rendered HTML keeps the frontend minimal — no React, no HTMX.

PicoCSS provides mobile-responsive styling on semantic HTML with no class names required. Forms, tables, buttons, and modals are styled automatically. Loaded via CDN (`<link>` in `base.html`). Custom overrides go in `static/style.css`.

---

## 2. Project Structure

```
project/
├── app/
│   ├── main.py              # FastAPI app entry point, startup events
│   ├── config.py            # Environment variable loading, app settings
│   ├── database.py          # SQLAlchemy engine, session factory
│   ├── models.py            # SQLAlchemy ORM models
│   ├── seed.py              # Seed event settings and booth types from config
│   ├── routes/
│   │   ├── vendor.py        # Vendor-facing routes (registration, dashboard)
│   │   ├── admin.py         # Admin dashboard routes
│   │   ├── auth.py          # Login/logout, OTP verification
│   │   └── webhooks.py      # Stripe webhook endpoint
│   ├── services/
│   │   ├── payment.py       # Stripe PaymentIntent creation, refunds
│   │   ├── email.py         # Resend email sending, template rendering
│   │   └── registration.py  # Registration status transitions, validation
│   ├── templates/           # Jinja2 HTML templates
│   │   ├── base.html
│   │   ├── vendor/          # Registration form steps, dashboard, payment
│   │   ├── admin/           # Admin dashboard pages
│   │   ├── auth/            # Login, OTP verification
│   │   └── emails/          # Email templates
│   └── static/              # CSS, JS, images
├── config/
│   └── event.json           # Event settings and booth type seed data
├── tests/
│   ├── test_registration_transitions.py
│   ├── test_webhooks.py
│   └── test_auth.py
├── data/
│   └── app.db               # SQLite database (auto-created, not committed)
├── .env                     # Environment variables (never committed)
├── .gitignore
├── requirements.txt
└── README.md
```

---

## 3. Database Schema

All models use SQLAlchemy ORM. SQLite in WAL mode for concurrent read access.

**Schema management:** `metadata.create_all()` on startup. Delete the database file to apply schema changes during development.

### 3.1 registrations

One row per registration. Combines vendor profile, booth selection, payment, and status.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | Integer | PK, auto-increment | Internal ID |
| `registration_id` | String(20) | Unique, not null, indexed | Human-readable (e.g., "ANM-2026-0042") |
| `email` | String | Not null, indexed | Login identifier |
| `business_name` | String | Not null | |
| `contact_name` | String | Not null | |
| `phone` | String | Not null | |
| `category` | Enum(food, non_food) | Not null | Set at registration; admin-changeable |
| `description` | Text | Not null | What they sell |
| `cuisine_type` | String | Nullable | Food vendors only |
| `needs_power` | Boolean | Default false | |
| `needs_water` | Boolean | Default false | |
| `needs_propane` | Boolean | Default false | |
| `booth_type_id` | Integer | FK → booth_types, not null | Vendor must select during registration |
| `status` | String(50) | Not null, indexed | pending, approved, rejected, confirmed, cancelled |
| `documents_approved` | Boolean | Default false | Informational only — admin tracks food vendor doc verification |
| `stripe_payment_intent_id` | String | Nullable | Populated after payment |
| `amount_paid` | Integer | Nullable | In cents; populated after payment |
| `refund_amount` | Integer | Default 0 | In cents |
| `approved_at` | DateTime | Nullable | When admin approved |
| `rejected_at` | DateTime | Nullable | When admin rejected |
| `rejection_reason` | String | Nullable | Optional reason from admin |
| `agreement_accepted_at` | DateTime | Not null | |
| `agreement_ip_address` | String | Not null | |
| `created_at` | DateTime | Not null, default now | |
| `updated_at` | DateTime | Not null, auto-update | |

### 3.2 booth_types

Seeded from `config/event.json` on first startup. Admin can adjust `total_quantity` via the dashboard; other fields require config change and redeploy.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | Integer | PK, auto-increment | |
| `name` | String | Not null | e.g., "Premium Booth" |
| `description` | Text | | |
| `total_quantity` | Integer | Not null | Admin-editable |
| `price` | Integer | Not null | In cents |
| `sort_order` | Integer | Default 0 | |
| `is_active` | Boolean | Default true | |

Availability derived from query — no counter column to maintain:

```sql
-- Occupied count (approved + confirmed)
SELECT COUNT(*) FROM registrations
WHERE booth_type_id = ? AND status IN ('approved', 'confirmed')

-- Available = total_quantity - occupied count
```

### 3.3 admin_users

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | Integer | PK, auto-increment | |
| `email` | String | Unique, not null | |
| `is_active` | Boolean | Default true | Deactivated, not deleted |
| `created_at` | DateTime | Not null, default now | |

### 3.4 otp_codes

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | Integer | PK, auto-increment | |
| `email` | String | Not null, indexed | |
| `code_hash` | String | Not null | HMAC-SHA256 (see §4.1) |
| `created_at` | DateTime | Not null, default now | |
| `expires_at` | DateTime | Not null | created_at + 10 minutes |
| `attempts` | Integer | Default 0 | Max 5 |
| `used` | Boolean | Default false | |

### 3.5 stripe_events

Webhook idempotency — prevents processing the same event twice.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | Integer | PK, auto-increment | |
| `stripe_event_id` | String | Unique, not null | |
| `event_type` | String | Not null | |
| `processed_at` | DateTime | Not null, default now | |

### 3.6 event_settings

Single-row table. Seeded from `config/event.json`. Registration dates are admin-editable via dashboard; all other fields require config change and redeploy.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | Integer | PK, default 1 | Always one row |
| `event_name` | String | Not null | |
| `event_date` | Date | Not null | |
| `registration_open_date` | DateTime | Not null | Admin-editable |
| `registration_close_date` | DateTime | Not null | Admin-editable |
| `vendor_agreement_text` | Text | Not null | |

---

## 4. Authentication

### 4.1 OTP Flow

1. User enters email.
2. Backend generates 6-digit code, stores HMAC-SHA256 hash + expiry in `otp_codes`.
3. Plaintext code sent via Resend.
4. If Resend API fails: display "We couldn't send the verification code. Please try again."
5. User enters code → backend compares HMAC using `hmac.compare_digest`.
6. On match: mark OTP as used, create signed session cookie.
7. On failure: increment `attempts`. After 5 failures, invalidate.

Rate limit: max 5 OTPs per email per hour.

### 4.2 Admin Login

On startup, app reads `ADMIN_EMAILS` and creates `admin_users` rows for new emails. After OTP verification, app checks `admin_users` (active) to grant admin access.

### 4.3 Sessions

- Signed cookies using `SECRET_KEY`. Flags: `HttpOnly`, `Secure`, `SameSite=Lax`.
- Vendor sessions: 24-hour inactivity timeout.
- Admin sessions: 8-hour inactivity timeout.
- Session contains: user type (vendor/admin), email, creation time, last activity.

---

## 5. Stripe Integration

### 5.1 Payment Flow

1. Vendor clicks payment link (only available for Approved registrations).
2. Backend creates Stripe `PaymentIntent` with booth price. ID saved on registration.
3. Frontend renders Stripe Elements card form using `client_secret`.
4. Vendor submits card → Stripe.js handles payment client-side.
5. Stripe sends `payment_intent.succeeded` webhook to `POST /api/webhooks/stripe`.
6. Webhook handler:
   - Verifies webhook signature
   - Checks idempotency (`stripe_events`)
   - Looks up registration by `stripe_payment_intent_id`
   - Updates status: Approved → Confirmed
   - Logs the transition to stdout
7. Confirmation email sent to vendor.

No inventory checks needed in the webhook — admin already verified availability before approving.

### 5.2 Refund Flow

1. Admin clicks "Cancel" on a Confirmed registration, enters refund amount.
2. Backend calls `stripe.Refund.create()` with PaymentIntent ID and amount.
3. On success: status → Cancelled, record `refund_amount`, log to stdout.
4. `charge.refunded` webhook handled idempotently (status already updated).

### 5.3 Configuration

| Variable | Purpose |
|----------|---------|
| `STRIPE_PUBLISHABLE_KEY` | Frontend — Stripe.js Elements |
| `STRIPE_SECRET_KEY` | Backend — PaymentIntents, refunds |
| `STRIPE_WEBHOOK_SECRET` | Backend — webhook signature verification |

**Local webhook testing:**

```bash
stripe listen --forward-to localhost:8000/api/webhooks/stripe
```

**Test cards:**

| Card Number | Result |
|-------------|--------|
| `4242 4242 4242 4242` | Succeeds |
| `4000 0000 0000 0002` | Declines |
| `4000 0000 0000 3220` | 3D Secure |

Live: replace test keys with live keys. No code changes.

---

## 6. Email Integration

All emails sent via Resend's Python SDK. A single `send_email()` function renders the Jinja2 template, calls Resend, and logs the result. On failure: log the error, never block the user flow — **except** OTP delivery (see §4.1).

| Variable | Purpose |
|----------|---------|
| `RESEND_API_KEY` | API authentication |
| `EMAIL_FROM` | Sender address |
| `APP_URL` | Base URL for links in emails |

---

## 7. Security

### 7.1 Input Validation

- All form inputs validated server-side.
- Jinja2 auto-escaping prevents XSS.
- Email and phone format validated.

### 7.2 CSRF Protection

- All state-changing requests (POST/PUT/DELETE) include a CSRF token.
- Signed token per session, embedded as hidden field in Jinja2 templates, validated server-side.
- Stripe webhook exempt (authenticated via signature).

### 7.3 Rate Limiting

| Endpoint | Limit |
|----------|-------|
| OTP requests | 5 per email per hour |
| Registration submissions | 10 per IP per hour |

### 7.4 Session Security

- Signed cookies with `SECRET_KEY`, `HttpOnly`, `Secure`, `SameSite=Lax`.
- HTTPS enforced (Railway provides TLS).

---

## 8. Error Handling

### 8.1 Inventory Management

Admin controls inventory by reviewing dashboard counts before approving registrations. No automated inventory enforcement needed. The dashboard shows derived counts (total, approved, confirmed, available) to support admin decisions.

### 8.2 Stripe Webhooks

- Signature verification on every webhook. Reject unverified with 400.
- Idempotency via `stripe_events` table.
- Supported events: `payment_intent.succeeded`, `charge.refunded`.
- Fallback: missed webhook → registration stays in current state; admin reconciles via Stripe Dashboard. Dashboard can show an indicator for Approved registrations that have a Stripe PaymentIntent but haven't transitioned to Confirmed.

### 8.3 External Service Failures

| Service | Failure | Handling |
|---------|---------|----------|
| **Stripe API** | Unreachable | "Payment processing temporarily unavailable. Please try again." |
| **Stripe Webhooks** | Missed/delayed | Registration stays in current state. Admin reconciles manually. |
| **Resend (transactional)** | API fails | Log failure. Never block user flow. |
| **Resend (OTP)** | API fails | "We couldn't send the verification code. Please try again." |

### 8.4 Error Display

- Vendor-facing: friendly, actionable messages. No stack traces.
- Admin-facing: more detail acceptable.

### 8.5 Logging

- Structured stdout logging (Railway captures automatically).
- Payment events always logged.
- Status transitions logged with before/after values and actor.
- Errors logged with timestamp, request context, stack trace (server-side only).

---

## 9. Deployment

### 9.1 Railway Setup

1. Push code to GitHub (excluding `.env`, `data/app.db`)
2. Connect repo to Railway
3. Add persistent volume mounted at `/data`
4. Set environment variables:
   - `STRIPE_PUBLISHABLE_KEY`
   - `STRIPE_SECRET_KEY`
   - `STRIPE_WEBHOOK_SECRET`
   - `RESEND_API_KEY`
   - `EMAIL_FROM`
   - `ADMIN_EMAILS` (comma-separated)
   - `APP_URL`
   - `SECRET_KEY` (for session signing)
   - `DATABASE_URL` (path to SQLite file on persistent volume)
5. Auto-deploys on push to main

### 9.2 Backup

Manual `sqlite3 app.db '.backup backup.db'` before the event. No automated backup needed for a single-event app.

### 9.3 Going Live Checklist

- [ ] Replace Stripe test keys with live keys
- [ ] Configure Stripe webhook endpoint for production URL
- [ ] Configure Resend with production sender domain
- [ ] Set strong `SECRET_KEY`
- [ ] Verify seed script populates correctly
- [ ] Verify admin accounts bootstrap
- [ ] Smoke test: full registration → admin approval → payment → confirmed (refund immediately)
- [ ] Verify emails deliver
- [ ] Document manual backup procedure
