# Vendor Registration App — Specifications

## Asian Night Market

**Prepared by:** Vietnamese American Community of West Tennessee
**Date:** February 2026
**Status:** Draft v15

> This document defines **what** the app does — business requirements, workflows, rules, and constraints. For technical implementation details, see [architecture.md](architecture.md). For the build plan, see [development_plan.md](development_plan.md).

---

## 1. Background

Previous approaches (Google Forms, EventHub) lacked payment integration and status tracking or were overpriced for ~150 vendors. This is a purpose-built, single-event registration system handling signup, payment, and approval. Developed with AI-assisted coding tools and deployed on a VPS.

---

## 2. Registration Workflow

### 2.1 Overview

One registration = one vendor + one booth. The process has three phases:

1. **Phase 1 — Registration:** Vendor creates a profile, selects a preferred booth type, and submits the registration. Status: **Pending**.
2. **Phase 2 — Admin Review:** Admin reviews the registration and approves or rejects it. Approved vendors receive an email with a payment link and deadline. Status: **Approved** or **Rejected**.
3. **Phase 3 — Payment:** Approved vendor pays via Stripe within the payment deadline. Status: **Paid**.

Registration opens and closes on dates set in the app configuration. Before the open date, the vendor-facing page displays "Registration opens on [date]." After the close date, it displays "Registration is closed."

If a vendor needs a second booth, they can register again or contact admin.

### 2.2 Phase 1 — Registration

The registration form is a two-step wizard. Progress is saved as a draft in the database (keyed by vendor email), so vendors can resume if their session expires.

**Step 1: All Registration Info**

A single form collects all required information:

- **Vendor Agreement:** Review and accept the Vendor Participation Agreement. Acceptance recorded with name, email, IP address, and timestamp. Agreement text is admin-editable via the Settings page.
- **Contact Info:** Business name, primary contact name, email (pre-filled from login, not editable), phone number.
- **Vendor Profile:** Category (Food, Beverage, Merchandise, Entertainment, Non-Profit, Health & Beauty, Promotion, Other), description of what the vendor intends to sell, electrical equipment needs (microwave, fryer, warmer, rice cooker, griddle, blender, or other with free-text option).
- **Address (food/beverage only):** Street address, city/state/ZIP — required for food permit auto-generation.
- **Booth Selection:** Vendor selects their preferred booth type. Available booth counts displayed. Booth types, descriptions, quantities, and prices configured via seed configuration and adjustable by admin.

| Booth Type | Description | Qty Available | Price |
|------------|-------------|---------------|-------|
| **Premium Booth** | Prime location (near entrance, corner, high foot traffic) | configurable | configurable |
| **Regular Booth** | Standard booth space | configurable | configurable |
| **Compact Booth** | Smaller footprint for vendors with minimal setup | configurable | configurable |

**Step 2: Review & Submit**

- Summary screen showing all entered information and selected booth type with price
- Vendor reviews and submits (no payment at this step)
- On submit: registration saved with status **Pending**; draft deleted

**Submission Confirmation Page**

After submitting, the vendor sees:

- Registration ID (e.g., "ANM-2026-0042")
- Booth type selected
- Message: "Your registration is under review. You'll receive an email when it's approved."
- Link to vendor dashboard

### 2.3 Phase 2 — Admin Review

Admin reviews pending registrations via the dashboard. For each registration, admin can:

- **Approve** — Booth price locked at approval time (`approved_price`). Payment deadline set (configurable, default 7 days). Vendor receives an email with the portal domain and deadline. Food/beverage vendors automatically get a pre-filled food permit PDF generated. Status transitions to **Approved**.
- **Reject** — Vendor receives a rejection notification with reason (required). Status transitions to **Rejected**.
- **Revoke approval** — Returns approved registration to Pending or Rejected. Cancels any active PaymentIntent first. Reason required.

Admin decides approvals based on dashboard inventory counts, vendor mix, and event needs.

**Payment deadline tracking:** Approved-but-unpaid registrations appear on the dashboard with urgency bands (normal → reminder 1 → reminder 2 → overdue). Admin can send customizable payment reminder emails (rate-limited to 1/hour). The system never auto-revokes — the admin decides whether to reclaim an overdue slot.

**Admin notes:** Admin can attach timestamped notes to any registration. Notes are visible on a dedicated notes page with sorting by date, registration ID, or concern flag. A concern/flag toggle marks registrations needing attention.

**Insurance document upload:** Vendors upload their Certificate of General Liability Insurance at `/vendor/insurance`. Insurance is per-vendor email (not per-registration) — one upload covers all of a vendor's registrations. Files are stored on disk in `uploads/insurance/`. Admins review and approve (or revoke approval of) uploaded documents from the registration detail page. Admin can also upload insurance on behalf of a vendor. Admin can send insurance reminder emails. Document approval is informational only — it does not affect registration status or block payment.

**Food permits:** When a food or beverage vendor is approved, a pre-filled Shelby County temporary food permit PDF is auto-generated using the vendor's registration data (business name, address, event details). Permits are stored in `data/permits/`. Vendors can download their permit from their registration detail page. Admin can regenerate permits or download all as a ZIP.

### 2.4 Phase 3 — Payment

Approved vendors receive an email with a link to the payment page. The payment page shows:

- Booth type and price
- Stripe Elements card form

On successful payment, the registration status transitions to **Paid** and a confirmation email is sent.

---

## 3. Payment

All payments are online via Stripe. No pay-by-check option. Payment is only available after admin approval.

- Card information collected via Stripe Elements (PCI-compliant; card data never touches our server).
- Backend creates a Stripe PaymentIntent for each approved registration. Amount = `approved_price` (locked at approval time) + processing fee.
- **Processing fee:** Configurable pass-through fee (default 2.9% + $0.30) that covers Stripe's fee so the organizer nets the full booth price. Formula: `(rate × price + flat) / (1 − rate)`. Admin-editable in Settings.
- On successful payment, the app updates registration status to Paid.
- Refunds processed through Stripe API, triggered by admin action (Paid → Cancelled only). Refund presets configurable (default: 100%, 75%, 50%, 25%, 0%).
- Stripe transaction ID stored on each registration for reconciliation.
- CSV export includes amount and status for every registration.
- No inventory race conditions — admin controls approvals based on dashboard availability counts.

---

## 4. Registration Statuses

### 4.1 Statuses

| Status | Meaning |
|--------|---------|
| **Pending** | Registration submitted, awaiting admin review |
| **Approved** | Admin approved; vendor notified to pay within deadline |
| **Rejected** | Admin rejected (vendor-facing label: "Declined") |
| **Paid** | Vendor paid; registration complete |
| **Cancelled** | Admin cancelled a paid registration (refund issued) |
| **Withdrawn** | Vendor voluntarily withdrew (vendor-facing label: "Withdrawn") |

A `refund_amount` field (in cents, default 0) records any refund issued on cancellation. A `reversal_reason` field stores the reason for any reversal action (reject, revoke, cancel, withdraw).

Insurance document approval is tracked in the separate `insurance_documents` table (per-vendor email). This is informational only — it does not affect registration status.

### 4.2 Valid Status Transitions

```
Pending → Approved        (admin approves)
Pending → Rejected        (admin rejects)
Pending → Withdrawn       (vendor withdraws)
Approved → Paid           (vendor pays via Stripe)
Approved → Rejected       (admin revokes approval before payment)
Approved → Pending        (admin revokes approval for re-review)
Approved → Withdrawn      (vendor withdraws)
Rejected → Pending        (admin revokes rejection for re-review)
Paid → Cancelled          (admin cancels + Stripe refund)
```

Status transitions are enforced in `app/services/registration.py`. Any transition not listed above is rejected. All transitions are logged.

---

## 5. Edge Cases and Business Rules

### 5.1 Approved Vendor Doesn't Pay

- Admin monitors approved registrations via the dashboard **Unpaid Registrations** section, which shows urgency bands (normal → reminder 1 → reminder 2 → overdue) based on configurable deadlines.
- Admin sends payment reminder emails directly from the dashboard (rate-limited to 1/hour per registration). Reminder templates are customizable in Settings.
- If the vendor doesn't pay by the deadline, admin can revoke approval (Approved → Pending or Rejected) to free the slot. The system never auto-revokes.

### 5.2 Vendor Rejected

- Vendor sees "Declined" status on their dashboard with the reason.
- If circumstances change, the vendor can submit a new registration.

### 5.3 Vendor Withdrawal

- Vendors can withdraw their own registration if it is Pending or Approved.
- If approved with an active PaymentIntent, the system cancels it before allowing withdrawal.
- Vendor receives a withdrawal confirmation email. Admin is notified.
- Withdrawn registrations free the booth slot.

### 5.4 Refund Requests

- Admin cancels the registration and selects a refund percentage (presets configurable in Settings).
- Refunds processed through Stripe API. Processing fee is not refunded.
- Only Paid registrations can be cancelled/refunded.

### 5.4 Category and Profile Changes

Category is set at registration. Admin can change it if needed. Vendors contact admin for any profile changes.

---

## 6. Admin Dashboard

### 6.1 Registration Management

- View all registrations in a table, filterable by status, category, booth type, insurance status, permit status, notes, and concern flag
- Search by vendor name, email, or registration ID
- Click into a registration for full details: profile info, booth type, payment, status, admin notes
- Approve or reject pending registrations
- Revoke approval (Approved → Pending or Rejected) or revoke rejection (Rejected → Pending) with reason
- Insurance document review: view uploaded documents, approve or revoke approval from registration detail (does not affect status). Admin can also upload insurance on behalf of a vendor.
- Cancel a paid registration with refund percentage selection (via Stripe) and reason
- Admin notes: attach timestamped notes to any registration. Concern/flag toggle for registrations needing attention.
- Food permit management: auto-generated on approval for food/beverage vendors, manual regeneration available, download individual or all as ZIP
- Export registrations to CSV (profile info, booth type, amount, status, notes, concern flag)

### 6.2 Unpaid Registrations & Payment Reminders

- Dashboard card shows all approved-but-unpaid registrations with urgency bands based on payment deadline
- Admin can send payment reminder emails directly from the dashboard (rate-limited to 1/hour per registration)
- Two reminder tiers with customizable subject and body templates in Settings
- Insurance reminder emails available for vendors missing documents

### 6.3 Inventory

- View inventory counts per booth type: total, pending, approved (pending payment), paid, available, plus revenue and refund totals
- Availability derived from registration statuses — no separate counter to maintain
- Admin uses these counts to decide whether to approve new registrations
- Bulk update: adjust total quantity and price for all booth types in a single form. Quantity cannot be set below reserved (approved + paid) count.

### 6.4 Event Configuration

All settings are admin-editable through the dashboard Settings page:

- Event name, start/end dates
- Registration open/close dates and times
- Vendor agreement text
- Homepage content (`front_page_content`) and banner text
- Contact email and developer contact
- Payment instructions and insurance instructions
- Processing fee (percentage + flat amount)
- Refund policy text and refund presets
- Payment deadline days (default 7) and reminder schedule (R1 and R2 days)
- Reminder email templates (subject and body, with template variables)
- Admin notification toggles: new registration, payment received, insurance uploaded

Booth types (name, description, base price) are seeded from `config/event.json`. Quantities and prices adjustable via the inventory view (see 6.3).

### 6.5 Financial Reconciliation

- **CSV export** includes amount, processing fee, refund amount, and status for every registration — import into a spreadsheet for totals and breakdowns.
- **Stripe Dashboard** provides transaction history, refund records, and payout reports.
- Stripe transaction IDs stored for cross-referencing.

### 6.6 What the Admin Dashboard Does NOT Include

These are handled with external tools at this scale (~150 vendors):

- **Bulk email** — Use Resend's dashboard or a mail merge.
- **Email history per vendor** — Check Resend's dashboard.
- **Analytics or reporting** — Export to CSV and use a spreadsheet.
- **Financial summary page** — Use CSV export + Stripe Dashboard.

---

## 7. Notifications and Emails

All emails sent via Resend. Templates defined in code as Jinja2 templates.

### 7.1 Email Triggers

| Trigger | Recipient | Content |
|---------|-----------|---------|
| Registration submitted | Vendor | Confirmation of submission, "under review" message, registration ID |
| Registration approved | Vendor | Approval notification with portal domain, payment deadline, and insurance instructions |
| Registration rejected | Vendor | Rejection notification with reason |
| Approval revoked | Vendor | Notification that registration is back under review |
| Payment confirmed | Vendor | Payment receipt, registration confirmed (status: Paid) |
| Refund processed | Vendor | Refund confirmation, amount, reason, processing fee note |
| Vendor withdrawal | Vendor | Withdrawal confirmation |
| Payment reminder | Vendor | Customizable reminder with deadline (admin-triggered, rate-limited) |
| Insurance reminder | Vendor | Reminder to upload insurance document (admin-triggered) |
| New registration | Admin (optional) | Notification with link to registration detail |
| Payment received | Admin (optional) | Notification with link to registration detail |
| Insurance uploaded | Admin (optional) | Notification with link to registration detail |
| Vendor withdrawal | Admin | Notification with link to registration detail |
| Alert: payment race | Admin | Payment received for non-approved registration |
| Alert: refund failure | Admin | Refund failed or succeeded but not recorded |
| Alert: dashboard refund | Admin | Refund issued outside the app |
| Alert: dispute filed | Admin | Chargeback filed, action required |

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
- Max 5 OTP requests per email per hour; max 20 OTP requests per IP per hour
- Max 3 failed verification attempts per code (previous codes invalidated on new request)
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
