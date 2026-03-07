# Workflow Assessment — Part 1 (Typical Workflows)

> Generated 2026-03-07. Reviews potential issues in the typical workflows documented in `docs/workflows.md` Part 1.
>
> All workflows (W1–W14) were verified against the codebase and are fully implemented as documented. This assessment identifies operational and design-level risks that the documentation does not cover.

---

## Issue Summary

| # | Workflow | Issue | Likelihood | Severity | Action Type |
|---|----------|-------|------------|----------|-------------|
| 1 | W1 | No duplicate registration warning | Medium | Low | Non-technical |
| 2 | W1 | Draft persistence without cleanup | Medium | Very Low | None needed |
| 3 | W1 | Rate limit (10/IP/hr) may block shared networks | Low | Medium | Non-technical |
| 4 | W2 | Accidental approval (no confirmation step?) | Low | Medium | Verify UI |
| 5 | W2 | Payment deadline not adjustable per-registration | Medium | Low | Non-technical |
| 6 | W2/W13 | ~~Food permit generated from unverified vendor data~~ | Medium | Medium | **Addressed** |
| 7 | W3 | ~~No self-service path for rejected vendors to re-apply~~ | Medium | Low | **Addressed** |
| 8 | W4 | ~~No downloadable receipt/invoice in app~~ | High | Medium | **Addressed** |
| 9 | W4 | Processing fee may surprise vendors at payment time | Medium | Low | Non-technical |
| 10 | W5 | Cancelled registrations don't notify waitlisted vendors | Low | Low | Non-technical |
| 11 | W6 | No deadline or enforcement mechanism for insurance | High | High | Non-technical policy |
| 12 | W6 | No "reject insurance with reason" flow | Medium | Medium | Non-technical |
| 13 | W8 | Price changes don't notify approved-but-unpaid vendors | Low | Low | Non-technical |
| 14 | W9 | Email deliverability issues may lock out vendors | Medium | High | Mixed |
| 15 | W10 | Withdrawal consequences may not be clear to vendor | Low | Medium | Verify UI |
| 16 | W11 | Manual reminder sending doesn't scale for batches | Medium | Low | Technical nice-to-have |

---

## Priority Tiers

| Priority | Issues | Recommended Action |
|----------|--------|--------------------|
| **Address soon** | #11 (insurance deadline), #14 (OTP deliverability) | Non-technical policy + verify technical config |
| **Monitor** | #4 (approval confirmation), #12 (insurance feedback) | Verify UI exists or add lightweight fixes |
| **Low priority** | #1, #3, #5, #9, #10, #13, #15, #16 | Non-technical admin awareness |
| **Ignore** | #2 (draft cleanup) | No action needed at this scale |

---

## Detailed Analysis

### W1. Vendor Registration

#### Issue 1: No duplicate registration warning

- **What:** A vendor can submit multiple registrations for the same booth type without any warning. They might accidentally double-register.
- **Likelihood:** Medium — especially with ~150 vendors, some may be unsure if their first submission went through (slow connection, no confirmation email received due to email delay).
- **Severity:** Low — admin can spot duplicates during review, but it creates unnecessary work.
- **Recommendation:** Non-technical. Admins should watch for duplicate submissions from the same email during review. Optionally, add a soft warning on the form ("You already have a pending registration for this booth type — are you sure?"), but this is low priority.

#### Issue 2: Draft persistence without cleanup

- **What:** If a vendor starts a registration but never submits (abandons at step 2), the draft persists in the DB forever.
- **Likelihood:** Medium — some vendors will start and abandon.
- **Severity:** Very Low — just stale rows in `registration_drafts`. No functional impact.
- **Recommendation:** No action needed. If the DB grows, a simple periodic cleanup script suffices. Not worth building now for ~150 vendors.

#### Issue 3: Rate limit (10/IP/hour) may block shared networks

- **What:** Multiple vendors at the same location (e.g., a food hall, shared workspace) share an IP. 10 submissions/hour could block legitimate vendors.
- **Likelihood:** Low — most vendors register from different locations. But possible at vendor meetups or if organizers help vendors register on-site.
- **Severity:** Medium — blocked vendors can't register, and the error may be confusing.
- **Recommendation:** Non-technical for now. If on-site registration is planned, admins should be aware of this limit. Technically, the limit could be raised or switched to per-email, but 10/IP/hour is generous for normal use.

---

### W2. Admin Approval

#### Issue 4: Accidental approval (no confirmation step?)

- **What:** If the admin clicks "Approve" by accident, the vendor gets an approval email and a payment link immediately. The admin can revoke (Approved -> Pending), but the vendor may have already seen the email or started paying — creating an E-A1 race condition scenario.
- **Likelihood:** Low — but accidents happen, especially on mobile.
- **Severity:** Medium — if the vendor pays before revocation, the admin must do a full Cancel & Refund.
- **Recommendation:** Verify that a confirmation dialog exists in the UI. If not, add one ("Are you sure you want to approve this registration?").

#### Issue 5: Payment deadline not adjustable per-registration

- **What:** `payment_deadline = approved_at + N days` (global config). An admin can't give one vendor more time without changing the setting for everyone.
- **Likelihood:** Medium — some vendors may ask for extensions (traveling, waiting on funds).
- **Severity:** Low — the system never auto-revokes on deadline, so a vendor can still pay late. But the dashboard shows them as "overdue," and the admin might mistakenly revoke their approval.
- **Recommendation:** Non-technical. Admins should know that deadlines are soft (no auto-revoke). If a vendor asks for an extension, the admin can simply not revoke. A per-registration deadline override would be nice but is low priority.

#### Issue 6: Food permit generated from unverified vendor data — ADDRESSED

- **What:** The permit PDF was auto-generated from vendor-submitted data at approval time. Typos in business name, description, etc. would produce incorrect permits.
- **Resolution:** Permits are no longer auto-generated on approval. Instead, the admin clicks "Generate Permit" which opens a dialog pre-filled with the registration data. The admin can review and correct fields (business name, contact name, phone, address, city/state/zip, food description, setup time) before generating the PDF. Corrected values are saved back to the registration record so subsequent regenerations use the fixed data.

---

### W3. Admin Rejection

#### Issue 7: No self-service path for rejected vendors to re-apply — ADDRESSED

- **What:** Once rejected, only an admin can revoke the rejection (Rejected -> Pending). The vendor can't self-service correct their application and resubmit.
- **Resolution:** The rejection email now explicitly invites vendors to contact the organizer (with a mailto link to `contact_email` from event settings) if they believe the rejection was in error or want to update their application and reapply. The admin can then revoke the rejection. A self-service resubmission flow is unnecessary for ~150 vendors.

---

### W4. Vendor Payment

#### Issue 8: No downloadable receipt/invoice in app — ADDRESSED

- **What:** Vendors had no downloadable receipt or invoice in the app after payment.
- **Resolution:** PDF invoices are now auto-generated on successful payment (via webhook). Invoices include organizer billing info (configurable in admin settings: org name, address, optional tax ID), vendor details, booth type, price breakdown, processing fee, total paid, and Stripe payment reference. Vendors can download from their registration detail page; admins can download per-registration or bulk-export as ZIP via the new Export dropdown on the registrations page. The payment confirmation email directs vendors to their dashboard to download the invoice.

#### Issue 9: Processing fee may surprise vendors at payment time

- **What:** The payment page shows `approved_price + processing fee`. Vendors may not expect the extra charge and may contact the organizer.
- **Likelihood:** Medium — especially if the fee isn't mentioned in the approval email.
- **Severity:** Low — communication issue, not a technical one.
- **Recommendation:** Non-technical. Verify the approval email template includes the total amount with fee breakdown, so vendors aren't surprised at payment time.

---

### W5. Admin Cancellation & Refund

#### Issue 10: Cancelled registrations don't notify waitlisted/pending vendors

- **What:** When a paid registration is cancelled, the booth slot becomes available again (derived count). But there's no notification to pending or rejected vendors who might want the slot.
- **Likelihood:** Low — cancellations should be rare.
- **Severity:** Low — the admin sees the freed slot on the dashboard and can manually reach out.
- **Recommendation:** Non-technical. Admins should check the dashboard after cancellations and manually approve any pending vendors who want the freed slot. No automated waitlist needed for ~150 vendors.

---

### W6. Vendor Insurance Upload

#### Issue 11: No deadline or enforcement mechanism for insurance

- **What:** Insurance is required but there's no deadline and no consequence in the system for not uploading it. A vendor can be "Paid" with no insurance document.
- **Likelihood:** High — vendors procrastinate. Insurance is often one of the last things vendors arrange.
- **Severity:** High — if vendors show up to the event without insurance, the organizer faces liability risk. The system doesn't block event participation based on insurance status.
- **Recommendation:** Non-technical primarily. Organizers should:
  1. Set a firm insurance deadline (communicated in approval and payment confirmation emails).
  2. Use the dashboard insurance stats to chase non-compliant vendors regularly.
  3. Use the "Send Insurance Reminder" button (W14) for individual follow-up.
  4. Consider adding a warning banner on the vendor dashboard for missing insurance (lightweight technical fix).
  5. Enforcement should remain manual — organizer decides policy (e.g., deny entry on event day without valid insurance).

#### Issue 12: No "reject insurance with reason" flow

- **What:** If the admin reviews insurance and it's insufficient (wrong coverage, expired policy), there's no "reject insurance with reason" flow. The admin can only approve or leave it unapproved.
- **Likelihood:** Medium — vendors may upload incorrect documents.
- **Severity:** Medium — the vendor doesn't know *why* their insurance wasn't approved. The admin must manually email them.
- **Recommendation:** Non-technical for now. Admin should use the notes feature (W12) to document why insurance is insufficient and email the vendor directly. For ~150 vendors, this is manageable. An insurance rejection flow with automated email would be a nice enhancement.

---

### W8. Admin Inventory Management

#### Issue 13: Price changes don't notify approved-but-unpaid vendors

- **What:** Price changes apply to future approvals only (`approved_price` is locked). This is correct behavior, but if an admin lowers the price, already-approved vendors pay the old (higher) price with no notification.
- **Likelihood:** Low — price changes after approvals start should be rare.
- **Severity:** Low — fairness concern if vendors compare prices.
- **Recommendation:** Non-technical. If a price decrease is needed, admins should decide whether to honor the old price or revoke/re-approve at the new price. Document this in the admin operations policy.

---

### W9. OTP Authentication

#### Issue 14: Email deliverability issues may lock out vendors

- **What:** If a vendor's email provider blocks or delays Resend emails (spam filter, greylisting), the vendor can't log in at all. OTP is the only primary auth method.
- **Likelihood:** Medium — corporate email filters and some providers (Yahoo, Hotmail) are aggressive with transactional email.
- **Severity:** High — the vendor is completely locked out of the system.
- **Recommendation:** Mixed.
  - **Non-technical:** Ensure Resend domain authentication (SPF, DKIM, DMARC) is properly configured. Include "check your spam folder" messaging on the login page.
  - **Technical:** Google OAuth is already implemented as a fallback — verify it's prominently offered on the login page as an alternative when OTP doesn't arrive. Consider adding a note like "Didn't receive the code? Try signing in with Google instead."

---

### W10. Vendor Withdrawal

#### Issue 15: Withdrawal consequences may not be clear to vendor

- **What:** A vendor withdrawing an approved registration loses their booth slot immediately. If they change their mind, they'd need to re-register and go through approval again (and the slot may be taken).
- **Likelihood:** Low — most vendors won't withdraw casually.
- **Severity:** Medium — irreversible from the vendor's perspective.
- **Recommendation:** Verify that the withdrawal confirmation dialog clearly states: "You will lose your booth reservation and must re-register if you change your mind. Your slot may no longer be available."

---

### W11. Payment Deadline & Reminders

#### Issue 16: Manual reminder sending doesn't scale for batches

- **What:** Admins must individually click "Send Reminder" for each overdue vendor. With multiple overdue vendors, this is tedious.
- **Likelihood:** Medium — if many vendors are approved in a batch, many may hit the deadline around the same time.
- **Severity:** Low — tedious but not broken.
- **Recommendation:** Technical nice-to-have: a "Send All Reminders" bulk action for overdue registrations. But for ~150 vendors (maybe 10–20 overdue at once), individual clicks are manageable. Low priority.
