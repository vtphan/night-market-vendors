# Workflow Analysis: Typical & Edge-Case Scenarios

> Generated 2026-03-01. Reference for test planning, QA, and resilience review.

---

## Part 1 — Typical (Essential) Workflows

These are the happy-path flows that ~95% of users follow.

### W1. Vendor Registration (Full Funnel)

| Step | Actor | Action | System Response |
|------|-------|--------|-----------------|
| 1 | Vendor | Visits `/vendor/register` | Shows agreement page (or "coming soon" / "closed" based on dates) |
| 2 | Vendor | Accepts agreement, enters name + email | Draft saved to `registration_drafts`; redirects to OTP login |
| 3 | Vendor | Receives OTP email, enters 6-digit code | Session created (signed cookie, 24h vendor timeout) |
| 4 | Vendor | Fills contact & profile (business name, phone, category, description, electrical needs) | Draft updated per step |
| 5 | Vendor | Selects booth type from available options | Draft updated; availability shown as derived count |
| 6 | Vendor | Reviews summary, submits | Registration created (`status=pending`, ID `ANM-YYYY-NNNN`); confirmation email sent; draft deleted |
| 7 | Vendor | Sees confirmation page with registration ID | Links to vendor dashboard |

**Preconditions:** Registration window open. Vendor not rate-limited (< 10 submissions / IP / hour).

---

### W2. Admin Approval

| Step | Actor | Action | System Response |
|------|-------|--------|-----------------|
| 1 | Admin | Logs in via OTP (email in `admin_users` table) | Admin session (8h timeout) |
| 2 | Admin | Opens dashboard, sees pending registrations | Counts, recent list, capacity alerts |
| 3 | Admin | Opens registration detail, clicks "Approve" | Locks `BoothType` row → checks inventory → sets `approved_price` → transitions to `approved` → post-commit verification reverts if concurrent approval caused overbooking |
| 4 | System | Sends approval email to vendor | Contains payment portal domain (not direct link) |

**Preconditions:** Available inventory > 0 for the selected booth type.

---

### W3. Admin Rejection

| Step | Actor | Action | System Response |
|------|-------|--------|-----------------|
| 1 | Admin | Opens pending registration, clicks "Reject" | Dialog prompts for reason (required) |
| 2 | Admin | Enters reason, confirms | Transitions to `rejected`; sends rejection email with reason |

---

### W4. Vendor Payment (Stripe)

| Step | Actor | Action | System Response |
|------|-------|--------|-----------------|
| 1 | Vendor | Logs in, opens approved registration | Payment form with Stripe Elements; shows `approved_price` + processing fee |
| 2 | Vendor | Enters card, clicks "Pay" | Frontend POSTs to `/vendor/registration/{id}/pay` → server creates/reuses PaymentIntent → returns `client_secret` |
| 3 | Frontend | Calls `stripe.confirmCardPayment()` | Stripe processes charge |
| 4 | Stripe | Sends `payment_intent.succeeded` webhook | Idempotency check (StripeEvent insert) → locks registration row → transitions `approved → paid` → records `amount_paid` |
| 5 | System | Sends payment confirmation email | Admin notification if enabled |
| 6 | Vendor | Redirected to dashboard | Status shows "Confirmed / Paid" |

**Preconditions:** Registration status is `approved`. PaymentIntent amount matches `approved_price` + fee.

---

### W5. Admin Cancellation & Refund

| Step | Actor | Action | System Response |
|------|-------|--------|-----------------|
| 1 | Admin | Opens paid registration, clicks "Cancel & Refund" | Dialog for refund amount (presets from refund policy) + reason |
| 2 | Admin | Enters amount ≤ (`amount_paid` − prior refunds), enters reason, confirms | **Step 1:** transition `paid → cancelled` committed to DB. **Step 2:** `stripe.Refund.create()` → record `refund_amount` → commit |
| 3 | System | Sends refund confirmation email to vendor | Includes reason and processing fee note |
| 4 | System | Sends admin alert email to all admins | Records who cancelled, reason, and refund amount for audit trail |

**Design note — commit-first ordering:** The cancellation is committed to the database *before* the Stripe refund is issued. This ensures the irreversible external operation (money movement) only happens after the app state is consistent. If the Stripe refund fails after the cancellation commit, the admin is alerted to issue the refund manually via Stripe Dashboard. This is strictly recoverable. The alternative (refund-first) risked an irrecoverable inconsistency: money refunded but the app still showing "paid".

---

### W6. Vendor Insurance Upload

| Step | Actor | Action | System Response |
|------|-------|--------|-----------------|
| 1 | Vendor | Navigates to `/vendor/insurance` | Shows upload form (or existing document status) |
| 2 | Vendor | Uploads PDF/PNG/JPG (≤ 10 MB) | Stored as `{uuid}.{ext}` in `uploads/insurance/`; DB record created; `is_approved` reset to `false` |
| 3 | Admin | Reviews on registration detail page, clicks "Approve" | Sets `is_approved=true`, records `approved_by` and `approved_at` |

**Note:** Insurance is per-vendor email, not per-registration. One upload covers all registrations.

---

### W7. Admin Dashboard Review

| Step | Actor | Action | System Response |
|------|-------|--------|-----------------|
| 1 | Admin | Opens `/admin/dashboard` | Shows: status counts, booth inventory, revenue, insurance stats, daily/hourly charts, capacity alerts |
| 2 | Admin | Filters registration list by status/category/booth type | Filtered view with search |
| 3 | Admin | Exports CSV | All registrations with sanitized fields (formula injection prevention) |

---

### W8. Admin Inventory Management

| Step | Actor | Action | System Response |
|------|-------|--------|-----------------|
| 1 | Admin | Opens `/admin/inventory` | Shows booth types with quantities, prices, reserved counts |
| 2 | Admin | Adjusts `total_quantity` | Validated: cannot set below reserved (approved + paid) count |
| 3 | Admin | Updates price | Applies to future approvals only; existing `approved_price` values unchanged |

---

### W9. OTP Authentication Flow

| Step | Actor | Action | System Response |
|------|-------|--------|-----------------|
| 1 | User | Enters email on login page | Rate-limit check (≤ 20 OTP/IP/hour, ≤ 5 codes/email/hour); admin emails validated against `admin_users` |
| 2 | System | Generates 6-digit code, HMAC-SHA256 hashes it, stores in `otp_codes` | All previous codes for that email invalidated |
| 3 | User | Enters code within 10 minutes | Timing-safe comparison; max 5 failed attempts per code; marks `used` on success |
| 4 | System | Creates signed session cookie | Role determined by `admin_users` table membership |

---

## Part 2 — Edge Cases

### Category A: Concurrency & Race Conditions

#### E-A1. Payment Succeeds While Admin Revokes Approval

**Trigger:** Admin clicks "Reject" (or revokes to pending) at the exact moment Stripe processes the vendor's payment.

**Sequence:**
1. Vendor submits card → Stripe begins charge
2. Admin revokes approval → attempts `PaymentIntent.cancel()`
3. Cancel arrives too late; Stripe has already captured the charge
4. Webhook `payment_intent.succeeded` fires
5. Handler finds registration status ≠ `approved`

**Outcome:** Registration set to `paid` anyway (no auto-refund). System appends note to `admin_notes`: `"[System MM/DD] Payment completed while status was '{old_status}'"`. Admin alert email sent immediately. Admin must manually review and cancel/refund if warranted.

**Why no auto-refund:** Refund decisions involve judgment (partial vs full, policy timing). Safer to alert a human than to issue a potentially wrong refund automatically.

---

#### E-A2. Duplicate Webhook Delivery

**Trigger:** Stripe retries a webhook (network timeout on first attempt, Stripe internal retry logic).

**Sequence:**
1. First delivery: `StripeEvent` inserted (flush, not commit), processing begins
2. Second delivery arrives before first commits
3. Second tries to insert same `StripeEvent` → `IntegrityError` (unique constraint on event ID)
4. Second handler catches error, rolls back, logs "duplicate", returns 200

**Outcome:** Exactly-once processing. Stripe sees 200 on both deliveries and stops retrying.

---

#### E-A3. Two Admins Approve the Last Booth Simultaneously

**Trigger:** Two admins view the same booth type showing 1 available, both click "Approve" on different pending registrations.

**Sequence (PostgreSQL — row-level locking):**
1. Admin A's request locks `BoothType` row (`SELECT ... FOR UPDATE`)
2. Admin B's request blocks on the same lock
3. Admin A: inventory check passes (1 available), approval committed, lock released
4. Admin B: lock acquired, inventory check now shows 0 available → `ValueError` raised

**Sequence (SQLite — post-commit verification):**
`FOR UPDATE` is a no-op on SQLite, so both requests can read the same stale count. A post-commit verification step detects and reverts the overbooking:
1. Admin A: reads count (1 available), commits approval
2. Admin B: reads count (1 available, stale), commits approval → overbooked
3. Admin B: post-commit re-read detects `available < 0` → reverts approval back to `pending` (with system reason) → `ValueError` raised
4. If both detect the overbook simultaneously, both revert; admin retries and one succeeds

**Outcome:** Only one approval succeeds. Second admin sees "No booths available (concurrent approval detected)" error. No approval email is sent for the reverted approval (email is queued only after the function returns successfully).

---

#### E-A4. Vendor Retries Payment During Webhook Processing

**Trigger:** Vendor clicks "Pay", sees spinner, gets impatient, refreshes and resubmits.

**Sequence:**
1. First attempt: PaymentIntent created, `stripe.confirmCardPayment()` called
2. Vendor refreshes: POST to `/pay` → server finds existing PI in reusable state, returns same `client_secret`
3. Frontend calls `confirmCardPayment()` again on same PI
4. Stripe deduplicates: if already succeeded, returns success without double-charging
5. Webhook fires once for the single successful charge

**Outcome:** No double-charge. Same PaymentIntent reused. Idempotent webhook processing.

---

#### E-A5. Registration ID Collision on Concurrent Submissions

**Trigger:** Two vendors submit at the same millisecond, sequential ID generation collides.

**Sequence:**
1. Both generate the same `ANM-YYYY-NNNN` ID
2. First insert succeeds
3. Second hits unique constraint → `IntegrityError`
4. Retry logic regenerates ID (up to 3 attempts)

**Outcome:** Both registrations created with unique IDs. Transparent to vendors.

---

### Category B: Stripe & Payment Edge Cases

#### E-B1. Stripe Refund Fails After Cancellation Committed

**Trigger:** Registration is cancelled and committed to DB, then `stripe.Refund.create()` fails (Stripe outage, network error, invalid PaymentIntent, etc.).

**Sequence:**
1. Registration transitioned to `cancelled` and committed to DB
2. Stripe refund API call fails — no money moves
3. DB rolled back for the refund_amount update only; cancellation persists
4. Admin alert email sent with PaymentIntent ID and instructions

**Outcome:** Registration correctly shows "Cancelled" in the app. No money has moved. Admin is alerted to issue the refund manually via Stripe Dashboard. This is strictly recoverable — the admin has the PaymentIntent ID and a clear error message. The previous approach (refund-first) risked an irrecoverable state where money was refunded but the app still showed "Paid".

---

#### E-B2. Refund Issued via Stripe Dashboard (Outside App)

**Trigger:** Admin (or Stripe support) issues a refund directly in the Stripe Dashboard, bypassing the app.

**Sequence:**
1. Stripe sends `charge.refunded` webhook
2. Handler fetches the charge from Stripe, reads cumulative `amount_refunded`
3. Updates `registration.refund_amount` from Stripe (authoritative source)
4. **Full refund** (refunded ≥ amount_paid): auto-transitions `paid → cancelled`, appends system note, sends admin alert
5. **Partial refund**: updates amount only, appends system note, sends admin alert (does NOT auto-cancel)

**Outcome:** App stays in sync with Stripe. Admin alerted for visibility. Partial refunds require manual follow-up.

---

#### E-B3. Chargeback / Dispute Filed

**Trigger:** Vendor's bank initiates a dispute (fraud claim, unrecognized charge, etc.).

**Sequence:**
1. Stripe sends `charge.dispute.created` webhook
2. Handler extracts dispute details (ID, amount, reason, deadline)
3. All admins alerted via email with full context and Stripe Dashboard link

**Outcome:** No automatic status change. Admin must respond to dispute in Stripe Dashboard before deadline. If dispute is lost, Stripe debits the merchant account.

---

#### E-B4. PaymentIntent Amount Mismatch After Price Change

**Trigger:** Admin changes booth price after a vendor's registration was approved but before payment.

**Sequence:**
1. Vendor approved at `$100` (`approved_price = 10000`)
2. Admin updates booth price to `$120`
3. Vendor visits payment page
4. Server uses `approved_price` (10000), not current price (12000)
5. PaymentIntent created for `$100 + fee`

**Outcome:** Vendor always pays the price locked at approval time. Price changes only affect future approvals.

---

#### E-B5. PaymentIntent Exists But Amount Changed (Fee Update)

**Trigger:** Admin changes processing fee percentage after a PaymentIntent was already created for a vendor.

**Sequence:**
1. Vendor visits payment page → PI created for `$100 + $3.20 fee = $103.20`
2. Admin changes fee from 2.9% to 3.5%
3. Vendor refreshes payment page
4. Server recalculates total: `$100 + $3.91 = $103.91` — mismatch with existing PI
5. Old PI cancelled, new PI created with correct amount

**Outcome:** Vendor sees updated total. No stale PaymentIntent used.

---

#### E-B6. Stripe Webhook Signature Verification Failure

**Trigger:** Request to webhook endpoint with invalid or missing Stripe signature (replay attack, misconfigured forwarding, etc.).

**Sequence:**
1. `stripe.Webhook.construct_event()` raises `SignatureVerificationError`
2. Handler returns HTTP 400

**Outcome:** Event rejected. No state changes. Stripe retries with valid signature if it was a transient issue.

---

### Category C: Authentication & Session Edge Cases

#### E-C1. OTP Brute-Force Attempt

**Trigger:** Attacker tries to guess a 6-digit code.

**Sequence:**
1. Each wrong guess increments `attempts` counter on `otp_codes` record
2. After 5 failed attempts: code permanently invalidated
3. Attacker must request new code (rate-limited: 5 codes/email/hour, 20 codes/IP/hour)

**Outcome:** At most 25 guesses per email per hour across 5 codes. Probability of guessing any single code: 5/1,000,000 = 0.0005%. Combined: ~0.00125% per hour. HMAC comparison is timing-safe.

---

#### E-C2. OTP Email Delivery Failure

**Trigger:** Resend API returns an error (bad email, rate limit, service outage).

**Sequence:**
1. OTP record created in DB
2. Email send fails
3. OTP record deleted from DB (doesn't waste rate-limit budget)
4. Vendor sees "We couldn't send the code" message with retry option

**Outcome:** No phantom OTP codes consuming rate limits. User can retry immediately.

---

#### E-C3. Session Expiry Mid-Action

**Trigger:** Vendor's session expires (24h inactivity) while filling out a multi-step form.

**Sequence:**
1. Vendor on step 3 of registration
2. Session middleware detects expired session on next request
3. Redirected to login page
4. After re-login, draft data is still in `registration_drafts` (keyed by email)
5. Vendor resumes from where they left off

**Outcome:** No data loss. Drafts persist independently of sessions.

---

#### E-C4. Admin Session Used from Different Context

**Trigger:** Admin logs in, shares the session cookie (or it's stolen).

**Mitigations:**
- Cookies are `HttpOnly` (no JS access), `Secure` (HTTPS only in production), `SameSite=Lax`
- 8-hour inactivity timeout
- Session is signed with `SECRET_KEY` (≥ 32 chars in production)
- No session invalidation mechanism (no "log out all sessions" — limitation)

---

### Category D: Data & Validation Edge Cases

#### E-D1. Registration Submitted at Closing Time

**Trigger:** Vendor clicks "Submit" at the exact moment the registration window closes.

**Sequence:**
1. Registration form rendered while window is open
2. Admin updates `registration_close_date` to now (or clock passes the deadline)
3. Vendor submits form
4. Server checks `registration_close_date` at submit time
5. Submission rejected; vendor redirected to "Registration closed" page

**Outcome:** Server-side check prevents late submissions. No race window (date checked at submit, not just at page load).

---

#### E-D2. Vendor Submits with Same Email Twice

**Trigger:** Vendor completes registration, then starts a new one with the same email.

**Outcome:** Allowed. The system supports multiple registrations per email (one vendor, multiple booths). Each gets its own registration ID and goes through the full workflow independently. Insurance is shared (per-email).

---

#### E-D3. Insurance Document Re-Upload

**Trigger:** Vendor uploads insurance, admin approves it, vendor uploads a new document.

**Sequence:**
1. New file stored with fresh UUID
2. Old file deleted from disk (after DB commit)
3. `is_approved` reset to `false`, `approved_by` and `approved_at` cleared
4. Admin notification sent (if enabled)

**Outcome:** New upload always requires fresh admin review. Old approval does not carry over.

---

#### E-D4. Admin Reduces Inventory Below Current Reservations

**Trigger:** Admin tries to set `total_quantity` to a number lower than currently reserved (approved + paid).

**Outcome:** Rejected. Validation prevents setting quantity below reserved count. Admin must cancel existing registrations first.

---

#### E-D5. Malicious File Upload as Insurance

**Trigger:** Attacker uploads a file with `.pdf` extension but malicious content.

**Mitigations:**
- Extension validated (`.pdf`, `.png`, `.jpg`, `.jpeg` only)
- Content-type checked
- Max size 10 MB
- Stored with UUID filename (unpredictable path)
- Path traversal rejected (no `..`, `/`, `\` in filename)
- Files stored outside web root
- Files served through authenticated download endpoint (not static serving)

---

#### E-D6. CSV Formula Injection

**Trigger:** Vendor enters `=CMD("calc")` as their business name, admin exports CSV.

**Mitigation:** Export sanitizes all fields: any cell starting with `=`, `+`, `-`, `@`, tab, or CR is prefixed with a single quote (`'`). Prevents spreadsheet formula execution.

---

### Category E: Infrastructure & Failure Modes

#### E-E1. Webhook Handler Crashes Mid-Processing

**Trigger:** Unhandled exception during webhook processing (bug, dependency failure).

**Sequence:**
1. `StripeEvent` record was flushed (not committed) at start of processing
2. Exception raised → entire transaction rolled back (including StripeEvent)
3. Handler returns HTTP 500
4. Stripe retries delivery (exponential backoff, up to 3 days)
5. On retry: event ID not in `stripe_events` table (rolled back), so it's processed as new

**Outcome:** Automatic retry via Stripe. No data corruption. Transient failures self-heal.

---

#### E-E2. SQLite Lock Contention Under Load

**Trigger:** Multiple simultaneous admin actions or webhooks on SQLite.

**Sequence:**
1. SQLite WAL mode allows concurrent reads but serializes writes
2. `busy_timeout=5000`: writers wait up to 5 seconds for lock
3. If still blocked after 5s: `OperationalError: database is locked`

**Outcome:** Rare under normal load (~150 vendors). Mitigated by WAL mode and busy timeout. SQLite is the production database; if lock contention becomes a recurring problem, migration to PostgreSQL is the fallback plan.

---

#### E-E3. Resend Email Service Outage

**Trigger:** Resend API is down or rate-limited.

**Sequence (non-OTP emails):**
1. Email send fails
2. Error logged
3. User flow continues unblocked (registration submits, approvals go through)
4. Vendor doesn't receive email but can check status on dashboard

**Sequence (OTP emails):**
1. Email send fails
2. OTP record deleted from DB
3. Vendor sees retry message
4. Must wait for Resend to recover

**Outcome:** Only OTP delivery is blocking. All other emails are fire-and-forget.

---

#### E-E4. Stripe Service Outage During Payment

**Trigger:** Stripe API or Elements JS unavailable.

**Sequence:**
1. If Stripe.js fails to load: card form doesn't render, vendor sees error
2. If PaymentIntent creation fails: server returns error, vendor sees "try again later"
3. If `confirmCardPayment` fails: frontend shows Stripe error message
4. No status change on registration (still `approved`)
5. Vendor can retry when Stripe recovers

**Outcome:** No partial state. Registration stays `approved` until payment fully succeeds via webhook.

---

#### E-E5. Server Restart During Active Session

**Trigger:** Server process restarts (deploy, crash, `uvicorn --reload`).

**Sequence:**
1. Sessions are cookie-based (signed, not server-stored) → survive restarts
2. In-memory OTP rate-limit counters reset → temporarily allows burst (but DB rate limits still enforced: 5 codes/email/hour)
3. Background tasks (email sends) that were in-flight are lost
4. Registration drafts are in DB → survive restarts

**Outcome:** Minimal disruption. Sessions persist. Drafts persist. Rate limits partially reset but DB constraints remain. Any in-flight emails may be lost (vendor can check dashboard).

---

## Part 3 — Summary Matrix

| ID | Scenario | Likelihood | Impact | Mitigation |
|----|----------|------------|--------|------------|
| E-A1 | Payment during approval revoke | Low | High (money moved) | Accept payment, alert admin, manual refund |
| E-A2 | Duplicate webhook | Medium | None | StripeEvent unique constraint |
| E-A3 | Concurrent last-booth approval | Low | Medium | Post-commit verification reverts overbooking (`FOR UPDATE` on PostgreSQL) |
| E-A4 | Vendor retries payment | Medium | None | PaymentIntent reuse, Stripe idempotency |
| E-A5 | Registration ID collision | Very Low | None | 3 retries on unique constraint |
| E-B1 | Stripe refund fails after cancel committed | Very Low | Low | Admin alert, manual refund via Stripe Dashboard (recoverable) |
| E-B2 | Dashboard refund (outside app) | Low | Medium | Webhook sync, admin alert |
| E-B3 | Chargeback/dispute | Low | High | Admin alert, manual response |
| E-B4 | Price change after approval | Medium | None | `approved_price` lock |
| E-B5 | Fee change after PI created | Low | None | PI cancelled and recreated |
| E-B6 | Invalid webhook signature | Low | None | Rejected with 400 |
| E-C1 | OTP brute-force | Low | Low | 5 attempts/code, rate limits |
| E-C2 | OTP email failure | Low | Medium | Delete OTP, show retry message |
| E-C3 | Session expiry mid-form | Medium | None | Draft persistence in DB |
| E-C4 | Session theft | Very Low | High | HttpOnly, Secure, SameSite, signed |
| E-D1 | Submit at closing time | Low | None | Server-side date check on submit |
| E-D2 | Same email, multiple registrations | Medium | None | By design — allowed |
| E-D3 | Insurance re-upload | Medium | None | Resets approval, admin notified |
| E-D4 | Inventory below reserved | Low | None | Validation rejects |
| E-D5 | Malicious file upload | Low | Low | Extension, type, size, path checks |
| E-D6 | CSV formula injection | Low | Medium | Cell prefix sanitization |
| E-E1 | Webhook handler crash | Low | Low | Stripe auto-retry up to 3 days |
| E-E2 | SQLite lock contention | Low | Low | WAL mode, 5s timeout; PostgreSQL fallback if needed |
| E-E3 | Email service outage | Low | Medium (OTP) / Low (other) | OTP: retry message. Others: fire-and-forget |
| E-E4 | Stripe outage during payment | Low | Low | No state change, vendor retries |
| E-E5 | Server restart mid-session | Low | Low | Cookie sessions, DB drafts, DB rate limits |
