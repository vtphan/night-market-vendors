# Development Progress

## Asian Night Market — Vendor Registration App

> Running changelog of what's been built, decisions made, and known issues.
>
> For what we're building: [spec.md](spec.md) · For how it's built: [architecture.md](architecture.md) · For the plan: [development_plan.md](development_plan.md)

---

## Changelog

<!-- Format: date, what happened, any decisions or blockers. Keep it simple. -->

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

**Next:** Phase 2 — Vendor Registration Form (multi-step form, agreement, booth selection, submission).

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
5. **Removed audit_log table.** Status transitions and admin actions logged to stdout (Railway captures automatically).

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
