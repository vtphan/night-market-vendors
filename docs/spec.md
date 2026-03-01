# Vendor Registration App — Specifications

## Asian Night Market

**Prepared by:** Vietnamese American Community of West Tennessee
**Date:** February 2026
**Status:** Draft v14

> This document defines **what** the app does — business requirements, workflows, rules, and constraints. For technical implementation details, see [architecture.md](architecture.md). For the build plan, see [development_plan.md](development_plan.md).

---

## 1. Background

Previous approaches (Google Forms, EventHub) lacked payment integration and status tracking or were overpriced for ~150 vendors. This is a purpose-built, single-event registration system handling signup, payment, and approval. Developed with AI-assisted coding tools and deployed on a VPS.

---

## 2. Registration Workflow

### 2.1 Overview

One registration = one vendor + one booth. The process has three phases:

1. **Phase 1 — Registration:** Vendor creates a profile, selects a preferred booth type, and submits the registration. Status: **Pending**.
2. **Phase 2 — Admin Review:** Admin reviews the registration and approves or rejects it. Approved vendors receive an email with a payment link. Status: **Approved** or **Rejected**.
3. **Phase 3 — Payment:** Approved vendor pays via Stripe. Status: **Confirmed**.

Registration opens and closes on dates set in the app configuration. Before the open date, the vendor-facing page displays "Registration opens on [date]." After the close date, it displays "Registration is closed."

If a vendor needs a second booth, they can register again or contact admin.

### 2.2 Phase 1 — Registration

**Step 1: Vendor Agreement**

All vendors must review and accept the Vendor Participation Agreement before proceeding. The agreement text is maintained in the app's configuration (requires redeploy to update). Acceptance is recorded with name, email, IP address, and timestamp.

**Step 2: Contact Info & Vendor Profile**

- Business or individual name
- Primary contact name
- Email address (verified via OTP before registration; used as login identifier)
- Phone number
- Vendor category: Food, Beverage, Merchandise, Entertainment, Non-Profit, Health & Beauty, Promotion, or Other
- Description of what the vendor intends to sell (vendors are prompted to include specifics like cuisine type)
- Electrical equipment needs (microwave, fryer, warmer, rice cooker, griddle, blender, or other)

**Step 3: Booth Selection**

| Booth Type | Description | Qty Available | Price |
|------------|-------------|---------------|-------|
| **Premium Booth** | Prime location (near entrance, corner, high foot traffic) | configurable | TBD |
| **Regular Booth** | Standard booth space | configurable | TBD |
| **Compact Booth** | Smaller footprint for vendors with minimal setup | configurable | TBD |

Dimensions and pricing configured before registration opens. The vendor selects their preferred booth type. Final booth assignment is at the organizer's discretion (per the vendor agreement).

Booth types, descriptions, quantities, and prices configured via seed configuration. Quantities adjustable by admin through the inventory view.

**Step 4: Review & Submit**

- Summary screen showing vendor profile and booth preference
- Vendor reviews and submits (no payment at this step)
- On submit: registration saved with status **Pending**

**Step 5: Submission Confirmation Page**

After submitting, the vendor sees:

- Registration ID (e.g., "ANM-2026-0042")
- Booth type selected
- Message: "Your registration is under review. You'll receive an email when it's approved."
- Link to vendor dashboard

### 2.3 Phase 2 — Admin Review

Admin reviews pending registrations via the dashboard. For each registration, admin can:

- **Approve** — Vendor receives an email with a link to the payment page. Status transitions to **Approved**.
- **Reject** — Vendor receives a rejection notification (with optional reason). Status transitions to **Rejected**.

Admin decides approvals based on dashboard inventory counts, vendor mix, and event needs. No automated inventory enforcement.

**Insurance document upload:** Vendors upload their Certificate of General Liability Insurance at `/vendor/insurance`. Insurance is per-vendor email (not per-registration) — one upload covers all of a vendor's registrations. Files are stored on disk in `uploads/insurance/`. Admins review and approve (or revoke approval of) uploaded documents from the registration detail page. Document approval is informational only — it does not affect registration status or block payment.

### 2.4 Phase 3 — Payment

Approved vendors receive an email with a link to the payment page. The payment page shows:

- Booth type and price
- Stripe Elements card form

On successful payment, the registration status transitions to **Confirmed** and a confirmation email is sent.

---

## 3. Payment

All payments are online via Stripe. No pay-by-check option. Payment is only available after admin approval.

- Card information collected via Stripe Elements (PCI-compliant; card data never touches our server).
- Backend creates a Stripe PaymentIntent for each approved registration.
- On successful payment, the app updates registration status to Confirmed.
- Refunds processed through Stripe API, triggered by admin action (Confirmed → Cancelled only).
- Stripe transaction ID stored on each registration for reconciliation.
- CSV export includes amount and status for every registration.
- No inventory race conditions — admin controls approvals based on dashboard availability counts.

---

## 4. Registration Statuses

### 4.1 Statuses

| Status | Meaning |
|--------|---------|
| **Pending** | Registration submitted, awaiting admin review |
| **Approved** | Admin approved; vendor notified to pay |
| **Rejected** | Admin rejected |
| **Confirmed** | Vendor paid; registration complete |
| **Cancelled** | Admin cancelled a confirmed registration (refund issued) |

A `refund_amount` field (in cents, default 0) records any refund issued on cancellation.

Insurance document approval is tracked in the separate `insurance_documents` table (per-vendor email). This is informational only — it does not affect registration status.

### 4.2 Valid Status Transitions

```
Pending → Approved        (admin approves)
Pending → Rejected        (admin rejects)
Approved → Confirmed      (vendor pays via Stripe)
Approved → Rejected       (admin revokes approval before payment)
Confirmed → Cancelled     (admin cancels + Stripe refund)
```

Status transitions are enforced in the backend. Any transition not listed above is rejected. All transitions are logged.

---

## 5. Edge Cases and Business Rules

### 5.1 Approved Vendor Doesn't Pay

- Admin monitors approved registrations via the dashboard.
- Admin contacts the vendor directly to follow up.
- If the vendor doesn't pay within a reasonable timeframe, admin can revoke approval (Approved → Rejected).

### 5.2 Vendor Rejected

- Vendor sees rejection status on their dashboard.
- If circumstances change, the vendor can submit a new registration.

### 5.3 Refund Requests

- Admin cancels the registration and enters the refund amount.
- Refunds processed through Stripe API.
- Only Confirmed registrations can be cancelled/refunded.

### 5.4 Category and Profile Changes

Category is set at registration. Admin can change it if needed. Vendors contact admin for any profile changes.

---

## 6. Admin Dashboard

### 6.1 Registration Management

- View all registrations in a table, filterable by status and category (food/non-food)
- Search by vendor name, email, or registration ID
- Click into a registration for full details: profile info, booth type, payment, status
- Approve or reject pending registrations
- Insurance document review: view uploaded documents, approve or revoke approval from registration detail (does not affect status)
- Cancel a confirmed registration with optional refund amount (via Stripe)
- Export registrations to CSV (profile info, booth type, amount, status)

### 6.2 Inventory

- View inventory counts per booth type: total, approved (pending payment), confirmed (paid), available
- Availability derived from registration statuses — no separate counter to maintain
- Admin uses these counts to decide whether to approve new registrations
- Adjust total quantity per booth type (e.g., if venue layout changes)

### 6.3 Event Configuration

Most settings configured via environment variables and seed config (requires redeploy):

- Event name and date
- Vendor agreement text
- Booth types: name, description, price (quantities adjustable via dashboard — see 6.2)

Admin-editable through the dashboard:

- Registration open date/time
- Registration close date/time

### 6.4 Financial Reconciliation

- **CSV export** includes amount and status for every registration — import into a spreadsheet for totals and breakdowns.
- **Stripe Dashboard** provides transaction history, refund records, and payout reports.
- Stripe transaction IDs stored for cross-referencing.

### 6.5 What the Admin Dashboard Does NOT Include

These are handled with external tools at this scale (~150 vendors):

- **Bulk email** — Use Resend's dashboard or a mail merge.
- **Email history per vendor** — Check Resend's dashboard.
- **Configurable email templates** — Templates defined in code (requires redeploy).
- **Admin settings UI (beyond registration dates)** — Managed via config files and env vars.
- **Analytics or reporting** — Export to CSV and use a spreadsheet.
- **Financial summary page** — Use CSV export + Stripe Dashboard.
- **Automated reminders** — Admin follows up manually or uses Resend for bulk sends.

---

## 7. Notifications and Emails

All emails sent via Resend. Templates defined in code as Jinja2 templates.

### 7.1 Email Triggers

| Trigger | Recipient | Content |
|---------|-----------|---------|
| Registration submitted | Vendor | Confirmation of submission, "under review" message, registration ID |
| Registration approved | Vendor | Approval notification with payment link and deadline |
| Registration rejected | Vendor | Rejection notification with optional reason |
| Payment confirmed | Vendor | Payment receipt, registration confirmed, next steps |
| Refund processed | Vendor | Refund confirmation, registration ID, amount |

### 7.2 Email Budget

~150 vendors, ~3-4 emails average per vendor → well under Resend's 3,000/month free tier.

---

## 8. Authentication

### 8.1 Vendor Authentication

Passwordless login via email + OTP. Vendors authenticate by entering their email and the 6-digit code sent to that email. If the email matches existing registrations, they see their dashboard. If not, they start a new registration.

If the OTP email fails to send, the system displays "We couldn't send the verification code. Please try again."

**Requirements:**

- 6-digit numeric OTP
- Expires after 10 minutes
- Max 5 OTP requests per email per hour
- Max 5 failed verification attempts per code
- Single-use
- Vendor sessions expire after 24 hours of inactivity

### 8.2 Admin Authentication

Admin accounts bootstrapped from `ADMIN_EMAILS` environment variable. Admins log in via the same OTP flow. The app checks `admin_users` to grant admin access. Admin sessions expire after 8 hours of inactivity.

Adding/removing admins: update `ADMIN_EMAILS` and restart. Removed emails are deactivated (not deleted).

---

## 9. Non-Functional Requirements

- **Mobile-friendly:** Vendor-facing flow must work on phones.
- **Low cost:** SQLite database, single VPS — a few dollars/month.
- **Simplicity:** As easy as a Google Form, but with payment integration and status tracking.
- **Reliability:** Admin controls approvals based on dashboard inventory counts. No automated inventory enforcement needed.
- **Data privacy:** Vendor data stored securely, access restricted to admins.
- **Data retention:** All vendor data deleted after the event. Admin downloads needed records (CSV export) beforehand. Recommended: within 90 days of event date.

---

## 10. Vendor Agreement

All vendors accept the Vendor Participation Agreement during registration. The agreement covers:

- Event date, location, and hours
- Setup and teardown schedule
- Booth assignment policy (locations assigned by organizers; vendors cannot request specific spots)
- Insurance requirements (food vendors only)
- Cancellation and refund policy
- Conduct, noise, and waste disposal rules
- Liability and hold-harmless clause
- Health department compliance (food vendors)
- Right of organizers to reject or revoke participation

The agreement text is maintained in the app's configuration (requires redeploy to update). Acceptance is recorded with name, email, IP address, and timestamp.

---

## 11. Open Questions

- [ ] Custom domain configuration?

**Resolved — to be set before launch:**

- Booth pricing and dimensions
- Vendor agreement text
