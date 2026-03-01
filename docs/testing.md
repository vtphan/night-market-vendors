# Test Strategy: User Story End-to-End Coverage

## Goal

Build a lightweight, reusable testing infrastructure so that any user story or workflow path through the app can be tested with a short, readable test — roughly 10–15 lines per story.

The app has two actors (vendor and admin), a state machine with seven transitions, Stripe payment integration, and an insurance document workflow. Today's tests cover individual steps well but don't chain them together into full user stories. We want to close that gap.

## Why This Matters

Every time code changes, we need confidence that the core user workflows still work end-to-end. Manual testing takes 30–45 minutes per round and can't easily cover Stripe webhooks or session expiry. Automated user story tests catch integration bugs — where individual steps pass but the handoff between them breaks.

## Status Terminology

The code uses lowercase status strings throughout. The spec and UI sometimes use different labels for the same status. This document uses the **code values** exclusively to avoid confusion:

| Code value | Spec / UI label | Meaning |
|------------|-----------------|---------|
| `pending` | Pending | Submitted, awaiting admin review |
| `approved` | Approved | Admin approved; awaiting payment |
| `rejected` | Rejected | Admin rejected |
| `paid` | Confirmed | Vendor paid; registration complete |
| `cancelled` | Cancelled | Admin cancelled + refund issued |

Tests must assert against the code values (e.g., `assert reg.status == "paid"`, not `"confirmed"`).

## State Machine Reference

The valid transitions defined in `app/services/registration.py` and the admin routes that trigger them:

```
pending   → approved    POST /admin/registrations/{id}/approve
pending   → rejected    POST /admin/registrations/{id}/reject
approved  → paid        Stripe webhook (payment_intent.succeeded)
approved  → rejected    POST /admin/registrations/{id}/reject
approved  → pending     POST /admin/registrations/{id}/unreject  (see note)
rejected  → pending     POST /admin/registrations/{id}/unreject
paid      → cancelled   POST /admin/registrations/{id}/cancel
```

**Note — Approved → Pending:** The state machine in `registration.py` allows `approved → pending`, and the `/unreject` route calls `transition_status(db, reg, "pending")`. Although the route name says "unreject," it works for any transition to `pending` that the state machine permits. The spec (§4.2) omits this transition — the spec should be updated to match the code.

## Current State

We have ~160 tests across 7 test files. They cover individual features thoroughly: OTP auth, form validation, state transitions, admin operations, Stripe webhooks, insurance, and error handling. The test infrastructure in `tests/helpers.py` provides data-seeding helpers (`seed_event_open`, `seed_booth_types`, `make_registration`, `seed_draft`) and auth helpers (`vendor_cookie`, `admin_cookie`, `extract_csrf`).

What's missing: composable **action helpers** for complex multi-step flows, and **end-to-end tests** that chain actions into full user story paths.

## The Approach: 4 Helpers + Inline Patterns

Keep the test infrastructure minimal. Four async helper functions handle the genuinely complex, frequently-used multi-step flows. Everything else uses explicit inline code following documented patterns.

### Part A — 4 Async Helpers (in `tests/helpers.py`)

These helpers exist because they each save 10+ lines per call and appear in 6+ stories. They handle CSRF extraction, cookie propagation, and mock coordination internally.

1. **`register_vendor(client, db, email, booth_type_id, cookies)`** — 35+ uses. Multi-step form flow: GET → CSRF → POST step1 → POST submit + email mock. The trickiest helper because it must capture updated session cookies after step 1 POST and use them for the review/submit step. Drafts are stored in the database via `RegistrationDraft`, not in session cookies, which simplifies cookie handling. Accepts optional overrides for `business_name`, `category`, etc. so tests can register multiple distinct vendors with the same email. Returns the pending registration.

2. **`approve_registration(client, db, registration_id, admin_cookies)`** — 24 uses. Admin approves a pending registration: GET detail → CSRF → POST approve + email mock + inventory lock. Returns the approved registration.

3. **`pay_registration(client, db, registration_id, vendor_cookies)`** — 9 uses. The most complex helper. Coordinates three mocked interactions: (a) GET the payment page, which triggers `PaymentIntent.create` server-side — mock to return a known `pi_test_xxx` ID; (b) POST the Stripe webhook endpoint with a constructed `payment_intent.succeeded` event using that same ID; (c) mock `stripe.Webhook.construct_event` to bypass signature verification. Returns the paid registration.

4. **`cancel_registration(client, db, registration_id, admin_cookies, refund_amount, reason)`** — Used less often but coordinates a refund mock + status transition + email mock in one atomic sequence. Admin cancels a paid registration: GET detail → CSRF → POST cancel + `stripe.Refund.create` mock + email mock. Returns the cancelled registration.

### Part B — Inline Patterns for Everything Else

Simple actions (reject, unreject, insurance ops) are single POST + CSRF — 4–6 lines of inline code. Webhook simulations use `build_webhook_event()` + inline mocking. See the **Inline Patterns** section below for copy-paste templates.

**Note on story descriptions:** `register_vendor`, `approve_registration`, `pay_registration`, and `cancel_registration` are helper function calls. All other action names in the stories below (e.g., `reject_registration`, `upload_insurance`, `unreject_registration`) describe inline operations following the patterns in the Inline Patterns section.

---

## Infrastructure Additions

### `tests/conftest.py`

Add the `client` fixture (async httpx test client):

```python
@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(app=app, base_url="http://test") as c:
        yield c
```

Note: `caplog` is a built-in pytest fixture — use it directly in test function signatures for Stories 41, 53, 54 that need log assertions.

### `tests/helpers.py`

Promote `build_webhook_event()` from `test_webhooks.py` to `tests/helpers.py` so it's reusable across test files:

```python
def build_webhook_event(event_id, event_type, data_object):
    """Build a Stripe webhook event dict for testing."""
    return {
        "id": event_id,
        "type": event_type,
        "data": {"object": data_object},
    }
```

---

## Mock Target Cheat Sheet

Reference table for the correct `@patch()` path for each external service. Derived from actual imports — always use these paths, never guess.

```
# Stripe SDK
webhook_construct     → app.routes.webhooks.stripe.Webhook.construct_event
charge_retrieve       → app.routes.webhooks.stripe.Charge.retrieve
pi_create             → app.services.payment.stripe.PaymentIntent.create
pi_retrieve           → app.services.payment.stripe.PaymentIntent.retrieve
pi_cancel (payment)   → app.services.payment.stripe.PaymentIntent.cancel
pi_cancel (reg svc)   → app.services.registration.stripe.PaymentIntent.cancel
pi_retrieve (reg svc) → app.services.registration.stripe.PaymentIntent.retrieve
refund_create         → app.services.payment.stripe.Refund.create

# Service functions (patch at import site in routes)
create_payment_intent → app.routes.vendor.create_payment_intent
create_refund         → app.routes.admin.create_refund

# Emails (patch at import site in routes)
submission_confirm    → app.routes.vendor.send_submission_confirmation_email
admin_notif (vendor)  → app.routes.vendor.send_admin_notification_email
approval              → app.routes.admin.send_approval_email
approval_revoked      → app.routes.admin.send_approval_revoked_email
rejection             → app.routes.admin.send_rejection_email
refund                → app.routes.admin.send_refund_email
admin_alert (admin)   → app.routes.admin.send_admin_alert_email
payment_confirm       → app.routes.webhooks.send_payment_confirmation_email
admin_notif (webhook) → app.routes.webhooks.send_admin_notification_email
admin_alert (webhook) → app.routes.webhooks.send_admin_alert_email

# OTP email
otp_email             → app.routes.auth.send_otp_email

# Resend SDK (low-level)
resend_send           → app.services.email.resend.Emails.send
```

---

## Inline Patterns

Three reusable inline patterns. Tests can copy-paste and adjust the route, mock target, and form data.

### Pattern A — Admin Single-Action (approve, reject, unreject, insurance ops)

```python
resp = await client.get(f"/admin/registrations/{reg_id}", cookies=acook)
csrf = extract_csrf(resp.text)
with patch("app.routes.admin.send_rejection_email"):
    resp = await client.post(
        f"/admin/registrations/{reg_id}/reject",
        data={"csrf_token": csrf, "reversal_reason": "Not a fit"},
        cookies=acook,
    )
assert resp.status_code == 303
db.refresh(reg)
```

### Pattern B — Webhook Simulation

```python
event = build_webhook_event("evt_test_001", "charge.refunded", {
    "id": "ch_test_xxx", "payment_intent": "pi_test_xxx",
    "amount": 15000, "amount_refunded": 15000,
})
with patch("app.routes.webhooks.stripe.Webhook.construct_event", return_value=event), \
     patch("app.routes.webhooks.stripe.Charge.retrieve") as mock_charge:
    mock_charge.return_value = MagicMock(amount_refunded=15000, payment_intent="pi_test_xxx")
    resp = await client.post("/api/webhooks/stripe",
        content=json.dumps(event), headers={"stripe-signature": "test_sig"})
```

### Pattern C — File Upload

```python
resp = await client.get("/vendor/insurance", cookies=vcook)
csrf = extract_csrf(resp.text)
with patch("app.routes.vendor.send_admin_notification_email"):
    resp = await client.post("/vendor/insurance/upload",
        data={"csrf_token": csrf},
        files={"file": ("insurance.pdf", b"%PDF-1.4 test", "application/pdf")},
        cookies=vcook)
```

---

## User Stories to Test (Ranked by Priority)

Each story below includes: what it tests, the actions needed, and what to verify. Stories are grouped into tiers by how likely/critical they are.

### Tier 1 — Core Paths (every successful vendor hits these)

**Story 1: Happy path — Register → Approve → Pay**
The primary workflow. Every vendor who successfully participates goes through exactly this path.
Actions: `register_vendor` → `approve_registration` → `pay_registration`
Verify: final status is `"paid"`, `amount_paid` is set, vendor dashboard shows the registration as confirmed.

**Story 2: Vendor checks status on dashboard**
After registering, vendors repeatedly check their dashboard before admin acts. The dashboard must show their registrations with correct statuses.
Actions: `register_vendor` → GET `/vendor/dashboard`
Verify: dashboard shows the registration ID with `"pending"` status. No other vendor's data is visible.

**Story 3: Admin reviews and approves**
Admin uses the dashboard to find pending registrations, views detail, and approves.
Actions: `register_vendor` → admin GET `/admin/registrations` → `approve_registration`
Verify: admin list shows the registration, detail page has correct vendor info, status changes to `"approved"`.

**Story 4: Vendor sees payment page after approval**
After approval, vendor follows the link to the payment page and sees the Stripe form.
Actions: `register_vendor` → `approve_registration` → vendor GET `/vendor/registration/{id}`
Verify: page contains the payment form, shows booth type and price.

### Tier 2 — Common Scenarios (happen regularly)

**Story 5: Admin rejects registration**
Admin decides against a vendor.
Actions: `register_vendor` → reject inline (Pattern A)
Verify: status is `"rejected"`, vendor dashboard shows rejection.

**Story 6: Same vendor registers for two booths**
Vendor wants both a food booth and a merchandise booth.
Actions: `register_vendor` (email=X, booth=A, business_name="Biz A") → `register_vendor` (email=X, booth=B, business_name="Biz B") → vendor GET `/vendor/dashboard`
Verify: dashboard shows two distinct pending registrations with different registration IDs and booth types.

**Story 7: Admin monitors inventory while approving**
Admin approves several vendors and inventory counts update correctly.
Actions: `register_vendor` (×3) → `approve_registration` (×3) → admin GET inventory page
Verify: available count for the booth type decreased by 3.

**Story 8: Vendor uploads insurance**
Food vendor uploads their Certificate of General Liability Insurance.
Actions: `register_vendor` → upload insurance inline (Pattern C) → vendor GET `/vendor/dashboard` or `/vendor/insurance`
Verify: insurance document exists, dashboard shows upload status.

### Tier 3 — Admin Corrections (happen occasionally)

**Story 9: Admin revokes approval before payment (Approved → Rejected)**
Admin approved too hastily or inventory situation changed.
Actions: `register_vendor` → `approve_registration` → reject inline (Pattern A, reason required)
Verify: status is `"rejected"`, `reversal_reason` is recorded, payment page is inaccessible (GET returns no payment form).

**Story 10: Admin revokes rejection for re-review (Rejected → Pending)**
Admin rejected but wants to reconsider.
Actions: `register_vendor` → reject inline → unreject inline (Pattern A, reason required)
Verify: status is `"pending"`, admin can now approve again.

**Story 11: Admin revokes approval for re-review (Approved → Pending)**
Admin approved prematurely and wants to put it back in the review queue without rejecting.
Actions: `register_vendor` → `approve_registration` → unreject inline (Pattern A, reason required)
Verify: status is `"pending"`, payment page is inaccessible, admin can re-approve or reject.

**Story 12: Full cancellation with refund (Paid → Cancelled)**
Vendor paid but needs to be cancelled (e.g., health department issue, vendor request).
Actions: `register_vendor` → `approve_registration` → `pay_registration` → `cancel_registration`
Verify: status is `"cancelled"`, `refund_amount` is recorded, vendor dashboard shows cancellation.

**Story 13: Admin adjusts inventory quantities**
Venue layout changes, admin increases booth count.
Actions: admin POST to inventory endpoint
Verify: available count reflects the new total. No helper needed — direct HTTP request.

**Story 14: Admin changes registration dates**
Push back the close date to allow more vendors.
Actions: admin POST to settings endpoint → vendor GET `/vendor/register` (previously closed, now open)
Verify: registration form is accessible after the date change. No helper needed.

### Tier 4 — Guard Rails and Edge Cases

**Story 15: Vendor can't pay before approval**
Vendor tries to access payment directly while still pending.
Actions: `register_vendor` → vendor POST `/vendor/registration/{id}/pay`
Verify: payment is rejected (400 or "not approved" error).

**Story 16: Vendor can't pay after revocation**
Admin approved, then revoked. Vendor still has the old payment link.
Actions: `register_vendor` → `approve_registration` → reject inline (Pattern A) → vendor POST pay endpoint
Verify: payment is rejected.

**Story 17: Inventory full — admin blocked from approving**
All booths of a type are taken.
Actions: seed booth type with quantity=2 → `register_vendor` (×3) → `approve_registration` (×2 succeed) → `approve_registration` (3rd fails)
Verify: third approval is blocked, available count is 0.

**Story 18: Dashboard reflects status at every stage**
Vendor dashboard updates correctly as registration moves through states.
Actions: `register_vendor` → check dashboard (`"pending"`) → `approve_registration` → check dashboard (`"approved"`) → `pay_registration` → check dashboard (`"paid"`)
Verify: each dashboard check shows the correct current status.

**Story 19: Insurance approval doesn't affect registration status**
Insurance is informational only. Approving it must not change the registration status.
Actions: `register_vendor` → `approve_registration` → upload insurance inline (Pattern C) → approve insurance inline (Pattern A)
Verify: registration status remains `"approved"` (unchanged by insurance approval). Insurance record shows `is_approved=True`.

**Story 20: Insurance revocation resets approval**
Admin can revoke a previously approved insurance document.
Actions: `register_vendor` → upload insurance inline (Pattern C) → approve insurance inline → revoke insurance inline (Pattern A)
Verify: insurance record shows `is_approved=False`, `approved_by=None`, `approved_at=None`. Registration status unchanged.

**Story 21: Insurance covers all vendor registrations**
One insurance upload per email, not per registration. If vendor has two registrations, one upload covers both.
Actions: `register_vendor` (email=X, booth=A) → `register_vendor` (email=X, booth=B) → upload insurance inline (Pattern C, email=X)
Verify: insurance document exists once for that email, accessible from both registration detail pages.

**Story 22: Rejected vendor resubmits**
Vendor was rejected, submits a fresh registration.
Actions: `register_vendor` (email=X) → reject inline (Pattern A) → `register_vendor` (email=X, different booth or details)
Verify: two registrations exist for the same email — one rejected, one pending.

**Story 23: Duplicate webhook doesn't double-process**
Stripe sends the same webhook twice.
Actions: `register_vendor` → `approve_registration` → `pay_registration` → send duplicate webhook inline (Pattern B, same payment_intent_id)
Verify: registration is `"paid"` (not errored), no duplicate processing, `stripe_events` table has one entry.

**Story 24: CSV export reflects all statuses**
Admin exports data and every registration shows correct status and payment info.
Actions: create one registration at each of the five statuses (`pending`, `approved`, `rejected`, `paid`, `cancelled`) using helpers + inline patterns → admin GET `/admin/export`
Verify: CSV contains exactly five rows with correct `status`, `amount_paid`, and `refund_amount` values for each.

**Story 25: Vendor blocked when registration is closed**
Registration period has ended; vendor-facing form is inaccessible.
Actions: `seed_event_closed` → vendor GET `/vendor/register`
Verify: page shows "Registration is closed" message, form is not rendered. POST to the register endpoint is also rejected.

### Tier 5 — Security and Auth Boundaries

**Story 26: Vendor can't access admin dashboard**
Vendor session tries to reach admin endpoints.
Actions: `register_vendor` → vendor GET `/admin/registrations`
Verify: returns 403 or redirects to login. No admin data is exposed.

**Story 27: Admin can't impersonate vendor actions**
Admin session tries to submit a vendor registration or access a vendor's payment page.
Actions: admin GET `/vendor/register`, admin POST to vendor submit endpoint
Verify: returns 403 or redirects. Admin session doesn't create registrations.

**Story 28: Unauthenticated user redirected to login**
No session cookie. Attempts to access protected routes.
Actions: GET `/vendor/dashboard`, GET `/admin/registrations`, GET `/vendor/registration/{id}` — all without cookies
Verify: all return 302 redirect to `/auth/login` (or equivalent).

**Story 29: CSRF token rejection**
POST a form with a missing or invalid CSRF token.
Actions: POST `/admin/registrations/{id}/approve` with no CSRF token, then with an invalid token
Verify: both return 403.

**Story 30: Session expiry enforcement**
Expired sessions are rejected.
Actions: create vendor cookie with `last_activity` older than 24 hours → GET `/vendor/dashboard`. Create admin cookie with `last_activity` older than 8 hours → GET `/admin/registrations`.
Verify: both redirect to login. Check `app/session.py` for the actual timeout mechanism before implementing.

**Story 31: OTP rate limiting**
Excessive OTP requests are blocked.
Actions: POST `/auth/request-otp` six times for the same email within one minute
Verify: first five succeed (200), sixth is rate-limited (429 or error message). Check `app/services/otp.py` for exact limit and response.

**Story 32: Registration submission rate limiting**
Excessive submissions from one IP are blocked.
Actions: `register_vendor` (×10 from same IP) → 11th `register_vendor`
Verify: 11th is rejected. Check `app/services/registration.py` `RATE_LIMIT_MAX` (currently 10) for exact threshold.

**Story 33: Invalid webhook signature rejected**
Stripe webhook with an invalid signature is rejected.
Actions: POST `/api/webhooks/stripe` with a valid-looking payload but without mocking `stripe.Webhook.construct_event` (so signature verification fails)
Verify: returns 400, registration status unchanged.

### Tier 6 — Google OAuth (when `GOOGLE_OAUTH_ENABLED=true`)

These tests require mocking Google's token and JWKS endpoints. They should be in a separate test file (`tests/test_google_oauth.py`) and skipped when OAuth env vars are not configured.

**Story 34: Vendor logs in via Google OAuth**
Happy path for Google sign-in.
Actions: GET `/auth/google?role=vendor` → verify redirect to Google with correct params → simulate callback with valid code and state cookie → mock token exchange and ID token verification
Verify: vendor session is created, redirect to `/vendor/dashboard`.

**Story 35: Admin logs in via Google OAuth**
Admin uses Google to authenticate.
Actions: GET `/auth/google?role=admin` → simulate callback with email matching `admin_users` table
Verify: admin session is created, redirect to `/admin/registrations`.

**Story 36: OAuth state cookie mismatch rejected**
Prevents CSRF in the OAuth flow.
Actions: GET `/auth/google` → simulate callback with a different or missing `oauth_state` cookie
Verify: returns error, no session created.

**Story 37: OAuth disabled returns error**
When `GOOGLE_OAUTH_ENABLED=false`, OAuth endpoints are inert.
Actions: GET `/auth/google` with OAuth disabled
Verify: returns 400 or appropriate error. No redirect to Google.

### Tier 7 — Concurrency, Payment, & Resilience Edge Cases

These stories cover the non-typical scenarios documented in `docs/workflows.md` (Categories A–E). They require targeted mocking but use the same helper + inline pattern approach. Each story references the corresponding edge-case ID from `docs/workflows.md`.

#### Concurrency & Race Conditions

**Story 38: Payment succeeds while admin revokes approval (E-A1)**
The highest-impact race condition. Admin revokes approval, but Stripe has already captured the charge.
Actions: `register_vendor` → `approve_registration` → simulate webhook for non-approved reg inline (Pattern B — set reg status to `"pending"` before sending `payment_intent.succeeded`)
Verify: registration status is `"paid"` (forced), `admin_notes` contains `"[System"` prefix, admin alert email triggered (mock assert). Repeat with status `"rejected"` to confirm same behavior.

**Story 39: Vendor retries payment — PaymentIntent reuse (E-A4)**
Vendor refreshes the payment page; the same PaymentIntent must be reused, not a new one created.
Actions: `register_vendor` → `approve_registration` → vendor POST `/vendor/registration/{id}/pay` (mock `PaymentIntent.create`, capture returned PI ID) → vendor POST `/vendor/registration/{id}/pay` again
Verify: `PaymentIntent.create` called only once. Second call reuses existing PI (same `client_secret` returned). Mock `PaymentIntent.retrieve` to return the PI in a reusable state (`requires_payment_method`).

**Story 40: Registration ID collision and retry (E-A5)**
Two registrations collide on the generated ID; the retry mechanism must recover.
Actions: Patch the ID generator to return a duplicate ID on the first call and a unique ID on the second → `register_vendor`
Verify: registration created successfully with the retried ID. No error surfaced to the vendor.

#### Stripe & Payment Edge Cases

**Story 41: DB commit fails after Stripe refund succeeds (E-B1)**
The most critical failure mode. Stripe refund goes through but the DB transaction fails — money left the account but the app still shows "Paid."
Actions: `register_vendor` → `approve_registration` → `pay_registration` → patch `session.commit` to raise `OperationalError` on the cancel route's commit → admin POST cancel inline (Pattern A) with refund amount and reason
Verify: `stripe.Refund.create` was called (refund issued), registration status is still `"paid"` (rollback), CRITICAL-level log emitted (`caplog` assert), admin alert email sent with Stripe Dashboard context.

**Story 42: Full refund via Stripe Dashboard auto-cancels (E-B2)**
Admin issues a full refund in the Stripe Dashboard; the `charge.refunded` webhook should auto-cancel the registration.
Actions: `register_vendor` → `approve_registration` → `pay_registration` → send `charge.refunded` webhook inline (Pattern B, refund_amount=amount_paid)
Verify: status transitions to `"cancelled"`, `refund_amount` updated to match Stripe, `admin_notes` contains system note, admin alert email sent.

**Story 43: Partial refund via Stripe Dashboard does not auto-cancel (E-B2)**
A partial refund from the Dashboard should update the amount but not change status.
Actions: `register_vendor` → `approve_registration` → `pay_registration` → send `charge.refunded` webhook inline (Pattern B, refund_amount=half of amount_paid)
Verify: status stays `"paid"`, `refund_amount` updated, `admin_notes` contains system note, admin alert email sent.

**Story 44: Chargeback / dispute triggers admin alert (E-B3)**
Vendor's bank files a dispute. The app must alert admins without changing registration status.
Actions: `register_vendor` → `approve_registration` → `pay_registration` → send `charge.dispute.created` webhook inline (Pattern B, with dispute data)
Verify: webhook returns 200, registration status unchanged (`"paid"`), admin alert email sent with dispute ID, amount, and reason.

**Story 45: Price change after approval doesn't affect vendor's charge (E-B4)**
`approved_price` locks the amount at approval time. Later price changes must not affect the vendor.
Actions: `register_vendor` → `approve_registration` (locks price at, e.g., $100) → admin POST to update booth price to $120 → vendor GET `/vendor/registration/{id}`
Verify: payment page shows $100 (the `approved_price`), not $120. Mock `PaymentIntent.create` and assert the amount argument uses `approved_price` + fee.

**Story 46: Fee change triggers PaymentIntent recreation (E-B5)**
If the processing fee changes after a PI was created, the old PI must be cancelled and a new one created.
Actions: `register_vendor` → `approve_registration` → vendor POST `/vendor/registration/{id}/pay` (PI created with original fee) → admin POST to update processing fee → vendor POST `/vendor/registration/{id}/pay` again
Verify: `PaymentIntent.cancel` called on the old PI, `PaymentIntent.create` called with the new amount. New `client_secret` returned.

#### Authentication & Session Edge Cases

**Story 47: OTP email delivery failure cleans up (E-C2)**
If the email send fails, the OTP record must be deleted so it doesn't consume the rate-limit budget.
Actions: mock Resend `emails.send` to raise an exception → POST `/auth/request-otp` with a valid email
Verify: no `otp_codes` record exists for that email in the DB. Response shows a retry-friendly message (not a 500). A subsequent OTP request succeeds (rate limit not consumed by the failed attempt).

**Story 48: Session expiry mid-form preserves draft (E-C3)**
Vendor's session expires while filling out the multi-step registration form. After re-login, their draft data is intact.
Actions: `register_vendor` (only steps 1–2, stop before submit) → create an expired vendor cookie (`last_activity` older than 24h) → GET `/vendor/register` with expired cookie → re-login via fresh `vendor_cookie` → GET `/vendor/register`
Verify: redirected to login on expired cookie. After re-login, draft data (business_name, category, etc.) is pre-populated from `registration_drafts`.

#### Data & Validation Edge Cases

**Story 49: Insurance re-upload resets admin approval (E-D3)**
A new upload must clear the previous approval so the admin reviews the new document.
Actions: `register_vendor` → upload insurance inline (Pattern C) → approve insurance inline (Pattern A) → upload insurance inline (Pattern C, new file)
Verify: `is_approved` is `False`, `approved_by` is `None`, `approved_at` is `None`. Only one `InsuranceDocument` record exists for the email (replaced, not duplicated).

**Story 50: Admin can't reduce inventory below reserved count (E-D4)**
Prevents overbooking by rejecting quantity reductions that would go below committed booths.
Actions: `register_vendor` (×2, same booth type) → `approve_registration` (×2) → admin POST to set `total_quantity` to 1
Verify: request rejected (error message about reserved count). Quantity unchanged at original value. Admin can set it to 2 (equal to reserved) but not lower.

**Story 51: Malicious file upload rejected (E-D5)**
Insurance upload must reject disallowed extensions, oversized files, and path-traversal filenames.
Actions: Three uploads via POST `/vendor/insurance/upload`:
  (a) file with `.exe` extension
  (b) file exceeding 10 MB
  (c) file with `../../etc/passwd` as filename
Verify: all three rejected. No file written to `uploads/insurance/`. No `InsuranceDocument` record created.

**Story 52: CSV export sanitizes formula injection (E-D6)**
Malicious cell content must be escaped to prevent spreadsheet formula execution.
Actions: `register_vendor` (business_name=`=CMD("calc")`, contact_name=`+HYPERLINK("http://evil")`) → `approve_registration` → admin GET `/admin/export`
Verify: CSV output contains `'=CMD("calc")` and `'+HYPERLINK("http://evil")` (prefixed with single quote). No raw `=` or `+` at cell start.

#### Infrastructure & Resilience

**Story 53: Webhook handler crash allows Stripe retry (E-E1)**
If the handler raises an unhandled exception, the StripeEvent record must be rolled back so Stripe can retry.
Actions: `register_vendor` → `approve_registration` → patch the webhook handler's internal processing to raise `RuntimeError` after StripeEvent flush → POST webhook inline (Pattern B)
Verify: returns 500. `stripe_events` table has no record for that event ID (rolled back). Registration status unchanged. A subsequent retry of the same event (without the patch) processes normally.

**Story 54: Email failure doesn't block registration or approval (E-E3)**
Non-OTP email failures must be logged but never block the user flow.
Actions: patch `email.send_email` to raise an exception → `register_vendor` → `approve_registration`
Verify: registration created (`"pending"`), approval succeeds (`"approved"`). No 500 errors. Email failure logged (`caplog` assert).

**Story 55: Stripe API failure during payment returns graceful error (E-E4)**
If `PaymentIntent.create` fails, the vendor sees a friendly error and registration stays `"approved"`.
Actions: `register_vendor` → `approve_registration` → mock `PaymentIntent.create` to raise `stripe.error.APIConnectionError` → vendor POST `/vendor/registration/{id}/pay`
Verify: response contains error message (not a 500 traceback). Registration status unchanged (`"approved"`). Vendor can retry later.

---

## Additional Test Gaps to Close

While building the above, also address these smaller gaps in existing tests. These can be additions to existing test files rather than new tests:

- **Email content verification**: In existing tests that mock email functions, add assertions that the email was called with the correct recipient and registration ID. Enhance tests in `test_webhooks.py` and `test_admin_routes.py`.
- **Agreement metadata**: In the submit test, verify that `agreement_accepted_at` and `agreement_ip_address` are populated. Enhance in `test_vendor_routes.py`.

---

## Helper ↔ User Story Coverage Matrix

| Helper | Stories | Count |
|--------|---------|-------|
| `register_vendor` | 1–12, 15–24, 26, 32, 38–46, 48–55 | 35+ |
| `approve_registration` | 1, 3, 4, 7, 9, 11, 12, 16–19, 24, 38, 39, 41–46, 50, 52–55 | 24 |
| `pay_registration` | 1, 12, 18, 23, 24, 41–44 | 9 |
| `cancel_registration` | 12, 24 | 2 |

All remaining stories use inline patterns (Pattern A for admin actions, Pattern B for webhooks, Pattern C for file uploads) or direct HTTP requests with targeted mocks. Stories 13, 14 are simple admin POSTs. Stories 25–33 use direct HTTP requests and seed helpers. Stories 34–37 use OAuth-specific mocking.

---

## Design Principles

- **4 helpers for complex flows, inline for the rest**: Helpers exist only where they save 10+ lines per use across 6+ stories. Everything else is explicit inline code.
- **No hidden abstractions**: Every test should be readable top-to-bottom without jumping to helper internals. The 4 helpers are the exception, justified by their frequency.
- **Mock targets from the cheat sheet**: Always reference the cheat sheet for `@patch()` paths. Never guess.
- **Assertions belong in tests, not helpers**: Helpers return objects. Tests assert on them. Helpers should assert only on HTTP status codes to catch setup errors early.
- **`caplog` for log assertions**: Stories 41, 53, 54 need the `caplog` fixture directly in the test function.
- **Read the actual route handlers first**: Before writing any helper or inline pattern, read the corresponding route in `app/routes/` to understand the exact mock targets, URL paths, and response patterns. Don't guess.
- **Use code status values everywhere**: Always assert against `"paid"`, `"cancelled"`, etc. — never the UI labels like "Confirmed" or "Cancelled."

## Known Gaps Not Covered Here

These are acknowledged limitations of the test strategy that would require additional infrastructure:

- **Concurrent approval race condition (E-A3)**: Two admins approving the last available booth simultaneously. The code uses `SELECT ... FOR UPDATE` to prevent this, but testing it requires concurrent database sessions with interleaved transactions. Consider a focused integration test using threading or `asyncio.gather` with separate DB sessions outside the action-helper framework.
- **End-to-end browser testing**: The action helpers test at the HTTP level. They don't exercise client-side JavaScript (Stripe Elements card input, form validation, payment.js error display). A future Playwright or Selenium suite could cover this.
- **SQLite lock contention under load (E-E2)**: WAL mode and `busy_timeout=5000` mitigate this, but reproducing `database is locked` errors requires sustained concurrent writes. Not cost-effective to test given production uses PostgreSQL. Accepted risk for dev/small deploys.
- **Server restart mid-request (E-E5)**: Cookie sessions and DB drafts survive restarts, but in-memory OTP rate-limit counters reset. Testing process restart behavior requires spawning/killing actual server processes, which is outside the scope of pytest-based testing.
- **Session theft / cookie exfiltration (E-C4)**: The mitigations (HttpOnly, Secure, SameSite, signed cookies) are configuration-level. Verifiable by inspecting response headers (partially covered by Story 30) but not by simulating an actual attack.

---

## How to Proceed

1. Add `client` fixture to `tests/conftest.py`.
2. Promote `build_webhook_event` from `test_webhooks.py` to `tests/helpers.py`.
3. Build the 4 helpers (`register_vendor`, `approve_registration`, `pay_registration`, `cancel_registration`) in `tests/helpers.py`. Test each with a simple story (Story 1) before building on it.
4. Write Tier 1–5 stories in `tests/test_user_stories.py`.
5. Write Tier 6 stories in `tests/test_google_oauth.py`.
6. Write Tier 7 stories in `tests/test_edge_cases.py`.
7. Add gap tests (email content, agreement metadata) to existing test files.
8. `pytest -v` after each step. Fix failures before moving on.

Expected outcome: ~60 new tests with 4 helpers + inline patterns.
