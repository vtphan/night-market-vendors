# Development Plan

## Asian Night Market — Vendor Registration App

> This document defines **when** things get built and **how we verify** each step. For business requirements, see [spec.md](spec.md). For technical details, see [architecture.md](architecture.md). For what actually happened, see [progress.md](progress.md).

---

## 1. Guiding Principles

### 1.1 Phase Discipline

Development is broken into 4 sequential phases. Each produces a working, testable increment. Phases are sequential in priority, but interleaving is fine when practical.

### 1.2 AI-Assisted Development Tips

- **Start with the data model.** Define SQLAlchemy models before building UI.
- **Build in vertical slices.** One complete workflow at a time (e.g., form → submit → admin review → approve).
- **Get admin review working early.** Core workflow — validate before building payment.
- **Keep state transitions explicit.** Enforce in a single service function.
- **Build admin dashboard alongside vendor flow.** Admin review is part of the core workflow, not a separate phase.
- **Test with Stripe test cards and CLI** when payment is integrated.

---

## 2. Testing Strategy

### 2.1 Approach

Manual smoke testing for UI flows. Automated tests for critical backend logic where correctness is essential and bugs would be hard to catch manually.

### 2.2 What Gets Automated Tests

| Area | What to Test | Why |
|------|-------------|-----|
| **Registration status transitions** | Every valid transition succeeds; every invalid transition rejected | State machine governs the entire app |
| **Stripe webhook handler** | Correct status updates, idempotency, signature rejection | Payment handling must be bulletproof |
| **OTP / auth** | Code generation, expiration, rate limiting, invalid code rejection | Auth bugs lock people out |

### 2.3 What Gets Manual Testing

- Full registration flow (fill form, submit, admin approves, pay with test card, receive confirmation)
- Vendor dashboard (login, view registrations and status)
- Admin dashboard (view/filter registrations, approve/reject, cancel/refund, CSV export)
- Mobile responsiveness

### 2.4 Test Tools

- **pytest** for automated tests
- **Stripe CLI** for webhook simulation
- **Stripe test cards** for payment outcomes
- **Browser DevTools** for responsive testing

### 2.5 Phase Exit Criteria

A phase is complete when:

1. All automated tests pass
2. All manual smoke tests pass
3. No known bugs in that phase's scope

---

## 3. Development Phases

### Phase 1 — Foundation, Admin Auth & Dashboard Shell

**Scope:** Project setup, database schema, seed script, admin bootstrap, OTP authentication, admin login, email service, protected dashboard routes.

**Deliverables:**

- FastAPI project structure matching architecture.md §2
- SQLAlchemy models for all tables (registrations, booth_types, admin_users, otp_codes, stripe_events, event_settings)
- Registration status values: pending, approved, rejected, confirmed, cancelled
- Database indexes on `registrations.status`, `registrations.registration_id`, `registrations.email`, `otp_codes.email`
- Schema auto-created via `metadata.create_all()` on startup
- SQLite in WAL mode
- Seed script (`app/seed.py`) populating event_settings and booth_types from `config/event.json`
- Admin account bootstrap from `ADMIN_EMAILS`; removed emails deactivated
- OTP login flow: email → 6-digit code → verify → session (shared by vendors and admins)
- OTP security: HMAC-SHA256 with SECRET_KEY, 10-minute expiry, rate limiting (5/email/hour), max 5 attempts per code, single-use
- OTP send failure handling: display retry message
- Admin login via OTP with `admin_users` check
- Session management (signed cookies, 8-hour admin / 24-hour vendor expiry, HttpOnly + Secure)
- Protected `/admin` routes
- Bare-bones admin dashboard page (placeholder, proves auth works)
- CSRF token middleware
- Email service (`app/services/email.py`): Resend integration, Jinja2 template rendering, failure logging (never block user flows except OTP)
- Structured stdout logging
- Health-check endpoint (`GET /health`)
- Environment variable loading (`app/config.py`)
- `.gitignore`, `requirements.txt`

**Test Criteria:**

- [ ] App starts and creates database with all tables
- [ ] Schema matches architecture.md §3
- [ ] Seed script populates event_settings and booth_types; running twice doesn't duplicate
- [ ] Admin accounts created on startup; running twice doesn't duplicate
- [ ] Removing email from `ADMIN_EMAILS` deactivates account
- [ ] OTP generation and HMAC verification works (automated)
- [ ] Expired OTP rejected (automated)
- [ ] Used OTP cannot be reused (automated)
- [ ] Rate limiting works (automated: max 5 attempts per code, max 5 codes per email per hour)
- [ ] OTP stored as HMAC, not plaintext (code review)
- [ ] OTP send failure shows retry message
- [ ] Admin can log in and access dashboard
- [ ] Non-admin email cannot access `/admin`
- [ ] Unauthenticated requests redirect to login
- [ ] Admin session expires after 8 hours
- [ ] CSRF token required on POST requests
- [ ] Email service sends via Resend; failures logged, don't block caller
- [ ] Health check returns 200

---

### Phase 2 — Vendor Registration & Admin Review

**Scope:** Registration form (creates registration with Pending status), submission confirmation, admin review (approve/reject), vendor dashboard, inventory view, CSV export.

**Deliverables:**

- Vendor agreement page (Step 1): accept/decline, records name, email, IP, timestamp
- Contact info & profile form (Step 2): name, email (pre-filled from OTP), phone, category, description, cuisine type, utility needs — server-side validation
- Booth selection (Step 3): vendor selects preferred booth type
- Review & submit page (Step 4): summary, no payment
- Registration saved with status **Pending** on submit
- Registration form rate limiting (10/IP/hour)
- Existing email detected → redirect to dashboard
- "Coming soon" page before registration open date/time
- "Registration closed" page after close date/time
- Submission confirmation page (Step 5): registration ID, "under review" message, dashboard link
- Submission confirmation email
- Vendor login → dashboard showing all registrations for their email with current status
- Admin registration list: filterable by status and category, searchable by name/email/registration ID
- Admin registration detail: full profile, booth type, status
- Approve button → status: Approved, approval email with payment link sent to vendor
- Reject button (with optional reason) → status: Rejected, rejection email sent to vendor
- Informational `documents_approved` checkbox for food vendor registrations
- Inventory view: total, approved (pending payment), confirmed (paid), available per booth type — derived from registration statuses
- Adjust `total_quantity` per booth type
- Registration date editing (admin can update open/close dates from dashboard)
- CSV export of registrations (profile info, booth type, amount, status)
- Mobile-responsive layout

**Test Criteria:**

- [ ] Full form flow completes: agreement → profile → booth → review → submit
- [ ] Server rejects invalid inputs (bad email, missing required fields)
- [ ] Existing email → redirected to dashboard
- [ ] Mobile-usable (manual)
- [ ] Agreement recorded with timestamp, name, email, IP
- [ ] "Coming soon" page shown before open date
- [ ] "Registration closed" page shown after close date
- [ ] Registration saved as Pending with correct data
- [ ] Submission confirmation page shows registration ID and "under review" message
- [ ] Submission confirmation email received
- [ ] Vendor can log in and see their registrations with status
- [ ] Admin can view, filter, and search registrations
- [ ] Admin can view registration detail
- [ ] Admin can approve → status changes to Approved, vendor notified with payment link (automated)
- [ ] Admin can reject → status changes to Rejected, vendor notified (automated)
- [ ] Admin can revoke approval before payment: Approved → Rejected (automated)
- [ ] Informational `documents_approved` checkbox does not affect status (automated)
- [ ] Inventory counts accurate (derived from registration statuses)
- [ ] Admin can adjust `total_quantity`
- [ ] Admin can update registration dates; changes take effect immediately
- [ ] CSV export correct and complete
- [ ] All status transitions follow allowed list — invalid transitions rejected (automated)
- [ ] Rate limiting rejects excessive submissions

---

### Phase 3 — Payment & Cancellation

**Scope:** Stripe payment for approved vendors, webhook handling, cancellation/refund flow.

**Deliverables:**

- Payment page for approved vendors: booth type, price, Stripe Elements card form
- Payment link in approval email leads to payment page
- Stripe PaymentIntent creation (only for Approved registrations)
- Stripe Elements card input
- Webhook for `payment_intent.succeeded`: Approved → Confirmed
- Webhook idempotency via `stripe_events`
- Declined card → graceful failure, retry possible
- Status update on payment: Approved → Confirmed
- Payment confirmation email
- Cancel confirmed registration with optional refund amount (via Stripe refund API)
- Refund email sent to vendor on cancellation
- `charge.refunded` webhook handler (idempotent)

**Test Criteria:**

- [ ] Only Approved registrations can access payment page
- [ ] Successful payment → status: Confirmed (automated via Stripe CLI)
- [ ] Declined card → graceful failure, retry possible
- [ ] Webhook processes correctly (Stripe CLI)
- [ ] Duplicate webhook idempotent (automated)
- [ ] Payment confirmation email received
- [ ] Confirmation page shows correct details
- [ ] Admin can cancel + refund → Stripe refund issued, email sent (automated)
- [ ] `charge.refunded` webhook idempotent (automated)
- [ ] Vendor dashboard shows updated status after payment

---

### Phase 4 — Hardening & Deployment

**Scope:** Error handling, error pages, polish, production deployment.

**Deliverables:**

- Error handling for Stripe API unreachable (friendly message)
- Error pages (404, 500, payment errors) — friendly messages, no stack traces
- Manual backup procedure documented in README
- Final responsive design pass
- Accessibility basics: form labels, focus states, color contrast
- Code pushed to GitHub
- Railway configured (persistent volume, env vars)
- Live Stripe keys and webhook endpoint
- Resend with production sender domain
- Admin accounts bootstrapped in production
- Seed script run in production
- Production smoke test: full registration + admin approval + real payment (refund immediately)

**Test Criteria:**

- [ ] Stripe failure shows friendly message
- [ ] All error pages show friendly messages (no stack traces)
- [ ] All pages render correctly on mobile
- [ ] End-to-end: register → admin approves → pay → confirmed
- [ ] App accessible at production URL
- [ ] Full flow works with real payment
- [ ] Webhook fires in production
- [ ] Admin can log in and see test registration
- [ ] Emails deliver from production
- [ ] HTTPS active
- [ ] Going Live Checklist (architecture.md §9.3) verified
