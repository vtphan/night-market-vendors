# Admin FAQ

## Getting Started

### How do I log in as an admin?

Go to the login page and enter your email. You'll receive a one-time passcode (OTP) — enter it to log in. There are no passwords. Your email must be listed in the admin users table to access the admin dashboard. The first set of admin emails is configured by the site developer via the `ADMIN_EMAILS` environment variable.

### What do the registration statuses mean?

| Status | Meaning | What to know |
|--------|---------|--------------|
| **Pending** | Vendor submitted a registration. Waiting for admin review. | Does not reserve a booth slot. No action needed from vendor. |
| **Approved** | Admin approved the registration. Vendor can now pay. | Immediately reserves a booth slot — even before the vendor pays. No auto-expiration. Monitor unpaid approvals. |
| **Rejected** | Admin rejected the registration. | No booth slot held. Can be reverted to Pending for re-review. |
| **Paid** | Vendor completed payment. Booth is confirmed. | Can only move to Cancelled (with optional refund). Cannot be reverted to Approved or Pending. |
| **Cancelled** | Admin cancelled a paid registration. | Booth slot freed. Refund issued through Stripe (if any). This is a terminal state. |

### How does the approval workflow work?

The system follows a strict sequence: **Register → Admin Approval → Payment**.

1. A vendor submits a registration (status: Pending).
2. An admin reviews and approves or rejects it.
3. If approved, the vendor receives an email with a payment link.
4. The vendor pays online via Stripe (status becomes Paid).

Vendors cannot pay until an admin approves their registration. This gives you full control over who gets a booth.

### How do I approve or reject a vendor?

Open the registration from the admin dashboard and use the Approve or Reject button. The vendor is emailed automatically when you take action.

### How do I know how many booth spots are left?

The admin dashboard shows availability counts for each booth type. Available = total quantity minus registrations in Approved or Paid status.

### Can a vendor submit multiple registrations?

**Yes.** There is no limit on registrations per vendor. A single email address can have multiple registrations for the same or different booth types. Each registration is tracked independently — you must approve, reject, or cancel each one separately. If a vendor submits duplicates by accident, reject the extras manually.

### What are the OTP and session limits?

OTP codes expire after **10 minutes**. Each code allows a maximum of 3 wrong attempts before it is locked — the vendor or admin must request a new code. A maximum of 5 codes can be requested per email per hour.

Admin sessions expire after **8 hours** or **1 hour of inactivity**, whichever comes first. Vendor sessions last 24 hours or 4 hours of inactivity.

### Is it safe for multiple admins to use the system at the same time?

**Yes.** Critical operations (approvals, cancellations, refunds) are protected against race conditions. If two admins try to approve the last available booth slot simultaneously, only one will succeed — the other will see an error message. It is safe to have multiple admins working concurrently.

---

## Inventory and Booth Management

### Do Pending registrations take up booth inventory?

**No.** Only Approved and Paid registrations count toward inventory. This means a booth type can appear to have open spots even if many vendors are waiting in Pending. Review and act on Pending registrations promptly to avoid over-committing.

### What happens when I approve a vendor?

Approving a vendor **immediately reserves a booth slot** for them — even before they pay. The available count for that booth type decreases by one. If this is the last available slot, no further approvals for that booth type are possible until a slot is freed (via rejection or cancellation).

### Can an approved vendor hold a spot without paying?

**Yes, and this is the biggest operational risk.** There is no automatic expiration on the Approved status. A vendor could sit in Approved indefinitely, taking up a booth slot without paying. Monitor approved-but-unpaid registrations and follow up or revoke approval if needed.

### What if a booth type is full and I need to approve another vendor?

You cannot approve a vendor for a booth type that has no available slots. To free a slot, you must reject or cancel an existing Approved or Paid registration for that booth type.

### How do I change booth types, prices, or quantities?

Go to **Admin Settings**. Each booth type can be edited for name, description, price, and total quantity. **Price changes only affect future approvals.** Already approved and paid registrations keep the price that was locked in at approval time. If you revoke an approval and re-approve, the new price will apply.

### Can I change a vendor's category after they register?

**Yes.** Open the registration detail page and use the category dropdown to correct it. Categories (Food, Beverage, Merchandise, Entertainment, Non-Profit, Health & Beauty, Promotion, Other) are available as filters on the registration list page for grouping vendors by type.

---

## Approvals, Rejections, and Status Changes

### Can I undo an approval?

Yes. You can revert an Approved registration back to **Pending** for re-review. This immediately frees up the booth slot that was reserved. You can also move it directly to Rejected.

### Can I undo a rejection?

Yes. You can revert a Rejected registration back to **Pending** for re-review.

### Can I undo a payment?

No — you cannot directly reverse the Paid status. The only path from Paid is **Cancelled**, which triggers a refund through Stripe. See the Refunds section below.

### What status transitions are allowed?

```
Pending  → Approved
Pending  → Rejected
Approved → Paid        (vendor pays)
Approved → Rejected
Approved → Pending     (revoke for re-review)
Rejected → Pending     (revoke for re-review)
Paid     → Cancelled   (admin cancels + refund)
```

Any transition not listed above is blocked by the system.

---

## Processing Fees

### How do processing fees work?

The system can pass Stripe's processing fee through to vendors so the organization nets the full booth price. When enabled, the vendor sees and pays: booth fee + processing fee. The fee is calculated using a pass-through formula that accounts for Stripe's percentage and flat fee.

### How do I configure the processing fee?

Go to **Admin Settings** under "Payment & Fees." You can set the percentage (default 2.9%) and flat amount (default 30 cents) to match Stripe's rates. To absorb fees yourself instead of passing them to vendors, set both to 0.

### When does the processing fee get calculated?

The processing fee is calculated when the vendor loads the payment page, using the **current** fee settings at that moment. Unlike the booth price (which is locked at approval time), the processing fee is not locked in advance. If you change the fee percentage, it affects any vendor who hasn't yet initiated payment.

### Is the processing fee refundable?

**No.** When you cancel a registration and issue a refund, the refund percentage applies only to the booth fee. The processing fee is never refunded to the vendor. This is shown clearly in the cancellation dialog.

---

## Payments and Refunds

### Do I need to collect payments manually?

No. Payments are handled entirely through Stripe. When you approve a vendor, they receive an email with a link to pay online. Card information is entered directly on Stripe's secure form and never touches our server.

### What happens when a vendor pays?

Stripe sends a webhook notification to the system, which automatically transitions the registration from Approved to Paid. The vendor receives a payment confirmation email.

### A vendor says they paid but the status still shows Approved. What happened?

There may be a short delay between Stripe processing the payment and the system receiving the webhook notification. Wait a few minutes and refresh. If the status still hasn't changed, check the Stripe Dashboard to confirm the payment succeeded, and contact the developer if reconciliation is needed.

### How do I issue a refund?

Open the Paid registration and use the Cancel button. You will be prompted to select a refund percentage and a reason. The refund is processed through Stripe automatically. The **refund percentage applies only to the booth fee** — the processing fee (shown separately in the dialog) is not refunded.

### Can I customize the refund percentage options?

**Yes.** Go to **Admin Settings** and edit the "Refund Presets" field. The default is `100,75,50,25,0`. For example, if your policy only allows full or no refund, set it to `100,0`. You can also set a "Refund Policy" text that is shown to admins in the cancellation dialog and to vendors on their registration page.

### Is there a way to cancel without issuing a refund?

Yes — select 0% when prompted for the refund percentage. The registration will be cancelled but no money is returned.

### Does a "100% refund" mean the vendor gets all their money back?

The vendor receives 100% of the booth fee back. The processing fee is not refunded. Additionally, the Stripe processing fee (2.9% + 30 cents) on the original charge is non-recoverable on your end — that cost is absorbed by the organization.

### Can I issue a refund directly in the Stripe Dashboard?

**Yes, but use caution.** Refunds made directly in Stripe are synced back to the app via webhook. A full refund in Stripe will automatically cancel the registration in the app. A partial refund updates the refund amount but does **not** change the registration status — it stays Paid. An alert email is sent to all admin addresses in either case.

### What happens if a vendor files a chargeback?

The system detects chargebacks via Stripe and sends an **urgent alert email** to all admin addresses with the dispute details. The registration status is not automatically changed. You must respond to the dispute directly in the Stripe Dashboard before the deadline (typically 7–21 days). Missing the deadline means you automatically lose the funds.

---

## Notifications and Email

### Which actions trigger an email to the vendor?

| Action | Email sent |
|--------|-----------|
| Vendor submits registration | Submission confirmation |
| Admin approves | Approval notification with payment link |
| Admin rejects | Rejection notification |
| Vendor pays | Payment confirmation |
| Admin cancels (with refund) | Refund confirmation |

### Can I receive notifications when vendors take actions?

**Yes.** Go to **Admin Settings** and enable the notification toggles under "Email Notifications." Three options are available:

- **New registration submitted** — notified when a vendor registers
- **Payment received** — notified when a vendor completes payment
- **Insurance uploaded** — notified when a vendor uploads an insurance document

These are **off by default**. Notifications are sent to all admin email addresses simultaneously.

### Can I prevent an email from being sent?

No. Vendor-facing emails (confirmations, approvals, rejections, refunds) are sent automatically and cannot be suppressed. Admin notification emails can be toggled off in Settings.

### A vendor says they didn't receive an email. What happened?

Email delivery failures are logged on the server but not surfaced in the admin dashboard. The email may have failed to send, landed in spam, or been delayed. Verify the vendor's email address on their registration and ask them to check their spam folder.

---

## Insurance

### How does insurance work in this system?

All vendors are required to have liability insurance. Vendors upload their insurance certificate through their registration page. Admins review and approve or revoke the document.

### Is insurance tied to a specific registration?

**No.** Insurance is tied to the vendor's **email address**. If a vendor has multiple registrations, a single insurance upload covers all of them. Similarly, revoking insurance affects all of that vendor's registrations.

### Is insurance approval separate from registration approval?

**Yes.** Approving a registration does not verify or approve the vendor's insurance. These are independent workflows. A vendor can be Paid with no insurance on file — it is the admin's responsibility to follow up on insurance compliance separately.

---

## Event Settings

### How do I update event dates, registration window, or page content?

Go to **Admin Settings**. You can edit the event name, dates, registration open/close dates, banner text, contact email, front-page content, vendor agreement, payment instructions, and insurance instructions.

### Do settings changes take effect immediately?

Yes. Changes are saved and reflected on vendor-facing pages immediately.

### What happens when registration closes?

Closing the registration window (past the close date) prevents new vendor registrations. However, **vendors can still log in** to check their existing registrations, and **approved vendors can still pay** after registration closes. Only new submissions are blocked.

### Where does the banner text appear?

Banner text appears at the top of the **homepage** and the **vendor dashboard** only. It does not appear on admin pages, the registration form, or confirmation pages. Use it for time-sensitive announcements vendors will see when they visit or log in (e.g., "Payment deadline is March 15").

### How do I export registration data?

Go to **/admin/export** (or use the Export link on the dashboard). The CSV includes **all registrations regardless of status** — pending, approved, rejected, paid, and cancelled. Columns include business name, contact info, booth type, category, insurance status, payment amounts, Stripe IDs, timestamps, reversal reasons, and admin notes. The file is named `registrations_YYYYMMDD.csv`.

Be mindful that admin notes and reversal reasons are included — review before sharing externally.

---

## Support

### How do I report a bug, request a feature, or give feedback?

Contact the app developer at the email listed in **Admin Settings** (Developer Contact field). Include a description of the issue or suggestion, along with any relevant screenshots or registration IDs to help with troubleshooting.
