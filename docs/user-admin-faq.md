# Admin FAQ

## Getting Started

### How do I log in as an admin?

Go to the login page and enter your email. You'll receive a one-time passcode (OTP) — enter it to log in. There are no passwords. Your email must be listed in the admin users table to access the admin dashboard. The first set of admin emails is configured by the site developer via the `ADMIN_EMAILS` environment variable.

### What do the registration statuses mean?

| Status | Meaning |
|--------|---------|
| **Pending** | Vendor submitted a registration. Waiting for admin review. |
| **Approved** | Admin approved the registration. Vendor can now pay. |
| **Rejected** | Admin rejected the registration. |
| **Paid** | Vendor completed payment. Booth is confirmed. |
| **Cancelled** | Admin cancelled a paid registration. Refund issued via Stripe. |

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

Go to **Admin Settings**. Each booth type can be edited for name, description, price, and total quantity.

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

## Payments and Refunds

### Do I need to collect payments manually?

No. Payments are handled entirely through Stripe. When you approve a vendor, they receive an email with a link to pay online. Card information is entered directly on Stripe's secure form and never touches our server.

### What happens when a vendor pays?

Stripe sends a webhook notification to the system, which automatically transitions the registration from Approved to Paid. The vendor receives a payment confirmation email.

### How do I issue a refund?

Open the Paid registration and use the Cancel button. You will be prompted to select a refund percentage (e.g., 100%, 75%, 50%, 25%, or 0%). The refund is processed through Stripe automatically.

### Is there a way to cancel without issuing a refund?

Yes — select 0% when prompted for the refund percentage. The registration will be cancelled but no money is returned.

### Does a "100% refund" mean the vendor gets all their money back?

The vendor receives 100% of the booth fee back. However, the Stripe processing fee (2.9% + 30 cents) is non-recoverable on your end — that cost is absorbed by the organization.

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

### Can I prevent an email from being sent?

No. Emails are sent automatically on the actions listed above.

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
