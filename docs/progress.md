# Development Progress

## Asian Night Market — Vendor Registration App

> Running changelog of what's been built, decisions made, and known issues.
>
> For what we're building: [spec.md](spec.md) · For how it's built: [architecture.md](architecture.md) · For the plan: [development_plan.md](development_plan.md)

---

## Changelog

<!-- Format: date, what happened, any decisions or blockers. Keep it simple. -->

### 2026-03-06 — Performance optimizations and code cleanup

Database query optimizations, caching, and code cleanup across the codebase.

**What changed:**

1. **N+1 query fix in inventory.** `get_inventory()` now uses a single aggregate query with GROUP BY instead of calling `_get_booth_counts()` per booth type.
2. **N+1 query fix in admin notes page.** Latest note per registration fetched via subquery join instead of per-registration queries. Insurance docs scoped to relevant emails only.
3. **EventSettings per-session cache.** New `get_event_settings(db)` in `app/database.py` caches the singleton EventSettings row for the lifetime of a DB session. Replaced ~40 direct `db.query(EventSettings).first()` calls across all route files.
4. **Database indexes.** Added indexes on `booth_type_id`, `stripe_payment_intent_id`, and composite index on `(agreement_ip_address, created_at)`.
5. **Shared upload constants.** New `app/upload_constants.py` consolidates file upload validation constants (extensions, content types, max size) previously duplicated in admin.py and vendor.py.
6. **Removed legacy code.** Deleted startup migration for `concern_status` column (already in schema). Removed unused `admin_notes` text column from Registration model.

---

### 2026-02-27 — Replace rejection_reason with universal reversal_reason

Replaced the `rejection_reason` column with `reversal_reason` so all reversal actions (reject, revoke approval, revoke rejection, cancel & refund) store a reason. All four actions now use a `<dialog>`-based two-step confirmation with preset reason dropdowns and a custom option. Reason is required for all reversal actions. Refund emails now include the cancellation reason.

**What changed:**
- `app/models.py`: Renamed `rejection_reason` → `reversal_reason`
- `app/services/registration.py`: Updated transition logic — sets `reversal_reason` on reject/revoke/cancel, clears on approve
- `app/services/email.py`: Added `reason` param to `send_refund_email()`
- `app/routes/admin.py`: Added `reversal_reason` validation to unreject/cancel routes, updated CSV export header
- `app/templates/admin/registration_detail.html`: Replaced inline confirm() with `<dialog>` modals for all reversal actions
- `app/templates/vendor/registration_detail.html`: Shows reason for both rejected and cancelled statuses
- `app/templates/emails/refund_confirmation.html`: Displays reason if provided
- Tests updated across all test files
- Schema change requires DB reset (`data/app.db` delete)

---

### 2026-02-25 — Google OAuth login + admin emails in settings

Added optional "Sign in with Google" as an alternative to OTP for both vendors and admins. Also added a read-only admin emails list to the settings page.

**What changed:**

1. **Google OAuth login.** New routes `GET /auth/google` and `GET /auth/google/callback` using `authlib`. State managed via signed cookie (no server-side sessions). Same role-based validation as OTP (admin emails checked against `admin_users`, vendors allowed when registration is open). Enabled when `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` env vars are set; otherwise hidden.
2. **Login page "Sign in with Google" button.** Conditionally shown below the OTP form when OAuth is enabled. Uses PicoCSS secondary button styling.
3. **Admin emails on settings page.** New read-only fieldset showing all emails configured in `ADMIN_EMAILS` env var. Display-only — not editable in the UI.
4. **9 new tests.** OAuth redirect, callback success (admin + vendor), admin rejection, invalid state, Google error, disabled state, button visibility.

**Decisions made:**

- OAuth state stored in a signed cookie with 5-minute expiry, not server-side sessions. Keeps the app stateless.
- Google OAuth is optional — OTP still works without Google credentials configured.
- `authlib` chosen for OAuth/OIDC handling (state/nonce management, ID token verification).

---

### 2026-02-25 — Insurance document upload feature

Replaced the informational `documents_approved` boolean on `registrations` with a dedicated `insurance_documents` table and file upload workflow.

**What changed:**

1. **Removed `documents_approved` column** from the `registrations` table.
2. **New `insurance_documents` table.** Columns: `id`, `email` (unique), `original_filename`, `stored_filename`, `content_type`, `file_size`, `is_approved`, `approved_by`, `approved_at`, `uploaded_at`. One document per vendor email — insurance is per-vendor, not per-registration.
3. **Vendor upload page at `/vendor/insurance`.** Vendors upload their Certificate of General Liability Insurance through the app instead of submitting externally via email or Google Drive.
4. **Admin approve/revoke from registration detail.** Admins review uploaded documents and approve or revoke approval directly from the registration detail page.
5. **File storage.** Uploaded files stored on disk in `uploads/insurance/`.

**Decisions made:**

- Insurance keyed by vendor email, not registration ID. One upload covers all of a vendor's registrations. This avoids duplicate uploads for vendors with multiple registrations.
- Document approval remains informational only — does not affect registration status or block payment.

---

### 2026-02-21 — Phase 2 complete: Vendor Registration, Admin Review & UX Improvements

Phase 2 built and verified. Vendor registration form, admin review workflow, homepage, and auth improvements all working end-to-end. 82 tests passing.

**What was built:**

1. **Vendor registration wizard (2 steps).** Step 1: all info (agreement, contact, profile, booth selection). Step 2: review & submit. Server-side validation on all steps. Registration draft stored in database across steps. Rate limiting (10/IP/hour).
2. **Registration service.** `app/services/registration.py` — state machine for all status transitions (Pending → Approved → Paid, with Rejected, Cancelled, and Withdrawn paths). Every invalid transition rejected. Registration ID generation (ANM-XXXXX format).
3. **Vendor dashboard.** Logged-in vendors see all their registrations with current status.
4. **Admin registration management.** List page with filter by status/category, search by name/email/ID. Detail page with full profile, approve/reject actions (reject with optional reason). Documents-approved checkbox (informational only).
5. **Admin inventory view.** Total, approved (pending payment), paid, and available counts per booth type. Admin can adjust `total_quantity`.
6. **Admin settings page.** Edit registration open/close dates and front page content.
7. **CSV export.** All registrations exported with profile info, booth type, amount, status.
8. **Email templates.** Submission confirmation, approval notification (with payment link placeholder), rejection notification. All via Resend.
9. **Public homepage at `/`.** Shows event name, date, registration status (coming soon / open / closed), and admin-editable front page content. Replaces the old redirect-to-login behavior.
10. **Role-based login flow.** `/auth/login` for vendors, `/auth/login?role=admin` for admins. Same email can log in as either role. Role carried through OTP flow via hidden form field.
11. **Admin email validation on login.** Non-admin emails are rejected before OTP is sent when using the admin login, preventing unauthorized OTP delivery.
12. **Logout bug fix.** Session refresh middleware was re-setting the cookie after `clear_session` deleted it. Fixed by skipping refresh on `/auth/logout`.
13. **Conditional vendor login visibility.** Vendor login link hidden in nav bar when registration is not open. Admin login always visible.
14. **`front_page_content` field.** Added to `event_settings` model. Admin-editable from settings page. Displayed on homepage.
15. **`is_registration_open()` method.** Added to `EventSettings` model as single source of truth for registration status checks.
16. **Tests.** 82 tests passing across 4 test files: auth (14), admin routes (25), vendor routes (24), registration transitions (19). Covers state machine, rate limiting, inventory counts, form validation, auth flows.

**Decisions made:**

- Homepage replaces redirect-to-login. Gives all visitors (vendors, admins, public) a clear landing page with event info and registration status.
- Role-based login via query parameter (`?role=admin`) instead of separate login pages or auto-detection from `admin_users` table. Simpler, and lets the same email test both roles.
- Admin email checked before OTP send, not after verification. Prevents wasting OTP codes and avoids confusing UX.
- Registration draft stored in `registration_drafts` table (keyed by vendor email). Drafts older than 24 hours cleaned up on startup.
- Resend domain verification needed for production email delivery (`nightmarketmemphis.com`). DNS records: DKIM (`resend._domainkey`), MX + SPF for bounce handling (`send` subdomain), DMARC (`_dmarc`).

**Verified:**

- Full registration form flow: agreement → profile → booth → review → submit
- Admin can view, filter, approve, reject registrations
- Inventory counts derived correctly from registration statuses
- Admin can edit registration dates and front page content
- Homepage shows correct registration status
- Vendor login hidden when registration not open; admin login always available
- Non-admin emails blocked at admin login before OTP sent
- Logout works correctly
- `pytest` → 82/82 pass

**Next:** Phase 3 — Payment & Cancellation (Stripe PaymentIntents, Elements, webhooks, refunds).

---

### 2026-02-21 — Phase 1 complete: Foundation, Admin Auth & Dashboard Shell

Project foundation built and verified. All code working end-to-end.

**What was built:**

1. **Project scaffolding.** `requirements.txt`, `.gitignore`, full directory structure per architecture.md §2.
2. **Config & database.** `app/config.py` loads all env vars from `.env`. `app/database.py` sets up SQLAlchemy with SQLite WAL mode.
3. **All 6 models.** `registrations`, `booth_types`, `admin_users`, `otp_codes`, `stripe_events`, `event_settings` — all columns, indexes, and constraints per architecture.md §3.
4. **Seed script.** `config/event.json` with event settings + 3 booth types (Premium $150, Regular $100, Compact $60). `app/seed.py` is idempotent. Admin bootstrap syncs `admin_users` with `ADMIN_EMAILS` env var on startup.
5. **CSRF protection.** Signed tokens via itsdangerous. Implemented as a FastAPI dependency (`require_csrf`) on POST routes rather than middleware — avoids Starlette's `BaseHTTPMiddleware` form body consumption issue.
6. **Session management.** Signed cookies via `itsdangerous.URLSafeTimedSerializer`. Vendor 24h / Admin 8h inactivity timeout. `HttpOnly`, `Secure` (when not DEBUG), `SameSite=Lax`. `require_admin` dependency checks session + `admin_users` table.
7. **OTP auth.** 6-digit codes, HMAC-SHA256 hashed, 10-minute expiry, max 5 attempts per code, max 5 OTPs per email per hour. Stored in `otp_codes` table.
8. **Email service.** Resend integration with separate Jinja2 environment for email templates. OTP email template built.
9. **Auth routes.** Login → OTP → Verify → Session creation. Redirects admin to `/admin`, vendor to `/vendor/dashboard`. Logout clears session.
10. **Admin dashboard.** Protected by `require_admin`. Bare-bones placeholder page.
11. **Main app.** Lifespan creates tables, seeds data, bootstraps admins. Health check at `/health`. Session refresh middleware.
12. **Tests.** 19 tests passing: OTP generation, HMAC roundtrip, expiry, reuse prevention, max attempts, rate limiting, health check, admin redirect, non-admin rejection, login page, OTP send flow.

**Decisions made:**

- `SECRET_KEY` generated and set in `.env`.
- CSRF implemented as dependency (not middleware) to avoid `BaseHTTPMiddleware` + form body conflict.
- `pytest.ini` added with `addopts = -p no:langsmith` to work around conda environment's `langsmith` plugin incompatibility.

**Verified:**

- `pip install -r requirements.txt` — no errors
- `uvicorn app.main:app --reload --port 8000` — starts, creates DB, seeds data
- DB has 1 event_settings row + 3 booth types
- `GET /health` → 200 OK
- `GET /admin` → 303 redirect to `/auth/login`
- `pytest` → 19/19 pass

**Next:** Phase 2 — Vendor Registration Form (multi-step form, agreement, booth selection, submission). ✅ Completed above.

---

### 2026-02-20 — Approval-first workflow (spec v14)

Switched from pay-at-registration to admin-approves-first workflow. All four docs updated.

1. **Approval-first workflow.** Admin must review and approve every registration before the vendor can pay. New three-phase flow: Register → Admin Review → Payment. Matches how curated food events actually work — organizers control the vendor mix.
2. **New status model.** Pending → Approved → Confirmed (with Rejected and Cancelled). Removed Draft and Paid statuses. Status transitions simplified.
3. **Removed auto-refund logic.** No inventory race conditions — admin controls approvals based on dashboard availability counts. Eliminated `reserved_count` column, CHECK constraint, and `services/inventory.py`.
4. **Inventory tracked via derived counts.** Available booths calculated from registration statuses (`approved` + `confirmed`), not a counter column. No atomic counter management needed.
5. **`documents_approved` is informational only.** Food vendor document tracking checkbox no longer triggers status transitions — it's a dashboard tracking tool for admin.
6. **Payment moved to Phase 3.** Phase 2 is now Registration + Admin Review. Phase 3 is Payment + Cancellation. Stripe is lower-risk without race conditions.
7. **New email triggers.** Added: submission confirmation, approval notification (with payment link), rejection notification. Removed: documents-approved email.
8. **New schema columns.** Added `approved_at`, `rejected_at`, `rejection_reason` to registrations. `booth_type_id` is now not null (no more drafts without booth selection).

### 2026-02-20 — MVP scope simplification (spec v13)

Five simplifications applied to reduce scope and cut cross-entity complexity. Development plan consolidated from 5 phases to 4. All four docs updated.

1. **Single booth per vendor.** Merged `vendors` + `orders` tables into a single `registrations` table. One registration = one vendor + one booth. Multi-order flow removed. If a vendor needs a second booth, they register again or contact admin.
2. **Removed document upload workflow.** Food vendors submit documents externally (email/Google Drive). Admin marks "documents approved" via a dashboard checkbox, transitioning Paid → Confirmed. Removed `documents` table; replaced with `documents_approved` boolean on registrations.
3. **Category set once, admin-changeable only.** Category is set at registration. Vendor-initiated category switching and its cascading side effects removed.
4. **Removed vendor self-service profile editing.** Vendors contact admin for profile changes.
5. **Removed audit_log table.** Status transitions and admin actions logged to stdout.

### 2026-02-20 — MVP review refinements (spec v12)

1. **Registration dates editable by admin via dashboard.** Registration open/close dates moved from config-only to admin-editable. All other event settings remain config-file-only.
2. **Confirmation page after payment.** Added Step 5 to registration flow: confirmation page showing registration ID, booth type, amount, next steps, dashboard link.
3. **Draft orders don't hold inventory (made explicit).** Inventory decremented only on successful payment via Stripe webhook. Now explicitly documented.
4. **Vendor can change category (food ↔ non-food).** Added automatic side effects for category switching. *(Removed in v13 — category now set once, admin-changeable only.)*

### 2026-02-20 — Vendors/orders split, multi-booth support (spec v11)

Split `registrations` into `vendors` + `orders` tables for multi-booth support. Added admin notification email for document uploads. Added OTP send failure handling. *(Reversed in v13 — merged back into single `registrations` table.)*

### 2026-02-20 — Removed waitlist feature (spec v10)

Waitlist removed from MVP scope. Merged Phase 4a/4b into Phase 4. Reduced from 6 phases to 5.

### 2026-02-20 — Scope refinements (spec v9)

Removed pay-by-check option. Added vendor self-service profile editing *(removed in v13)*. Redesigned waitlist as dual-purpose *(removed in v10)*. Cut all HTMX — standard form submissions only.

### 2026-02-20 — MVP simplifications (spec v8)

Unified OTP auth for vendors and admins. Collapsed registration form from 5 steps to 4. Manual backup instead of automated cron. Alembic removed from scope.

### 2026-02-20 — Pre-development setup

- Stripe sandbox and Resend free tier accounts created. Keys stored in `.env`.
- Using real Stripe test mode and Resend free tier from Phase 1 (no mocks).
- Schema changes: delete `app.db` and re-run `create_all()` + seed script.
- `STRIPE_WEBHOOK_SECRET` to be set during Phase 2 (Stripe CLI for local webhooks).
- `SECRET_KEY` and `ADMIN_EMAILS` to be set at Phase 1 start.
