# Admin Operations Policy

**Asian Night Market — Vendor Registration App**

Welcome! This document covers the ground rules for admins so everything runs smoothly — especially around money.

Think of this as our playbook. If something weird happens, check here first.

---

## 1. The Golden Rules

These are the most important things to remember:

1. **All refunds go through the app.** Never issue a refund directly in the Stripe Dashboard. The app tracks who cancelled, why, and notifies the vendor automatically. Going through Stripe directly skips all of that.

2. **Read every alert email.** The system sends alert emails when something unusual happens (a payment came in at an unexpected time, a refund failed, a vendor filed a chargeback). These are not informational — they require your attention and often your action.

3. **When in doubt, don't rush.** If you're unsure whether to approve, reject, cancel, or refund — wait. A registration sitting in Approved for an extra day is harmless. An accidental refund is not. Payment deadlines exist to nudge vendors, but the system never auto-revokes — you always have time to decide.

4. **Stripe Dashboard is the source of truth for money.** The app tracks what *should* have happened. Stripe tracks what *actually* happened. If there's ever a discrepancy, trust Stripe.

---

## 2. Day-to-Day Operations

### Reviewing and Approving Registrations

- **Pending registrations don't reserve a booth slot.** Only Approved and Paid registrations count toward inventory. Don't let a long Pending queue create a false sense of scarcity — check the actual available count on the dashboard.

- **Approving immediately reserves a booth slot** — even before the vendor pays. Be intentional about approvals when inventory is low.

- **Payment deadlines are tracked but not auto-enforced.** When you approve a vendor, a payment deadline is set (configurable in Settings, default 7 days). The **Unpaid Registrations** section on the dashboard shows all approved-but-unpaid vendors, color-coded by urgency: blue (normal), amber (past reminder 1), orange (past reminder 2), red (overdue). Use the inline **Remind** button to send email reminders (rate-limited to 1 per hour). To reclaim an overdue slot, use **Revoke Approval** with the "Payment deadline expired" preset — the system never auto-revokes.

- **Price is locked at approval time.** If you change a booth price in Settings, it only affects future approvals. Already-approved vendors still pay the price they were approved at. If you need someone to pay the new price, revoke their approval and re-approve them.

### When Booth Types Are Almost Full

- **Coordinate with other admins.** When a booth type has 3 or fewer slots left, let the other admin(s) know before approving. The system prevents overbooking, but if two admins click "Approve" at the same instant, one will see an error and have to retry. Not a crisis, just an annoyance you can avoid with a quick message.

- **Don't approve-and-reject rapidly to "test" the system.** Each approval triggers an email to the vendor. Rapid toggling is confusing for vendors and creates unnecessary payment-intent activity in Stripe.

### Revoking an Approval

Before you reject or revert an approved registration back to Pending, check one thing:

**Has the vendor started paying?** Look at the registration detail page — if there's a Stripe PaymentIntent ID listed, the vendor may have already visited the payment page. In this case:

- If they're actively paying right now, the system will block your revoke and tell you the payment is in progress. Wait for it to finish, then use Cancel & Refund.
- If they visited the payment page but didn't complete payment, the system will cancel the pending payment before revoking. This is safe.

The key takeaway: if a vendor is mid-payment, don't force a status change. Let the payment finish, then cancel and refund through the normal flow.

---

## 3. Payments and Financial Vigilance

### When a Vendor Pays

1. Stripe processes the charge and sends a notification to our app.
2. The app automatically moves the registration from Approved to Paid.
3. The vendor and (optionally) admins receive confirmation emails.

**This is fully automatic.** You don't need to do anything. But during the first week of operations, it's a good idea to spot-check:

- Open the [Stripe Dashboard](https://dashboard.stripe.com) and compare a few recent payments against what the app shows.
- Make sure the amounts match (booth price + processing fee).
- If a vendor says they paid but the app still shows Approved, wait a few minutes — there can be a short delay. If it persists, check Stripe directly.

### Issuing a Refund (Cancel & Refund)

This is the most sensitive admin action. Here's exactly what happens:

1. You click Cancel on a Paid registration, select a refund percentage, enter a reason.
2. The app first saves the cancellation to our database (status becomes Cancelled).
3. Then the app tells Stripe to send the money back.
4. Vendor and all admins are emailed automatically.

**The refund percentage applies to the booth fee only.** The processing fee is not refunded to the vendor. This is shown in the cancellation dialog.

**What to watch for after cancelling:**

- **Success (normal case):** You'll see the registration as Cancelled with the refund amount recorded. The vendor gets an email. Done.
- **"Refund failed" error:** The cancellation went through but Stripe couldn't process the refund (rare — usually a Stripe outage). You'll see a red error banner and receive an alert email. **You need to issue the refund manually in the Stripe Dashboard** and notify the vendor yourself. The alert email includes the PaymentIntent ID and vendor email to help you do this.
- **"Failed to record" error:** The refund was actually issued (money moved) but the app couldn't save the amount. **Do NOT issue another refund** — the vendor already got their money back. The system will self-correct within a few hours via Stripe's webhook. You'll get an alert email confirming this.

### Verifying Refunds in Stripe

After any refund — especially if you saw an error — verify it in the Stripe Dashboard:

1. Go to [Stripe Dashboard > Payments](https://dashboard.stripe.com/payments).
2. Search for the PaymentIntent ID (starts with `pi_`, shown on the registration detail page).
3. Confirm the refund amount and status match what the app shows.

**Do this routinely for the first few refunds** until you're confident the system is working correctly.

### What If Someone Refunds Through Stripe Directly?

If a refund is issued in the Stripe Dashboard instead of through the app:

- **Full refund:** The app detects it via webhook, automatically cancels the registration, and sends an alert email to all admins flagging it as unexpected.
- **Partial refund:** The app records the refund amount but does NOT cancel the registration. An alert email is sent.

In both cases, the vendor is NOT notified automatically (because the app doesn't know the context). The alert email will remind you to notify them.

**Bottom line:** Always use the app's Cancel & Refund. If you must use Stripe directly (e.g., the app is down), notify the other admins and follow up with the vendor manually.

---

## 4. Responding to Alert Emails

The system sends alert emails for situations that need human attention. Here's what each one means and what to do:

### "Payment received for non-approved registration"

**What happened:** A vendor's payment went through at the exact moment an admin was changing their status (extremely rare). The system accepted the payment to avoid losing track of money.

**What to do:**
1. Open the registration (link in the email).
2. Read the system note in Admin Notes — it explains the original status.
3. If the vendor should keep their booth, no action needed.
4. If not, use Cancel & Refund.

### "Refund failed"

**What happened:** The registration was cancelled but Stripe couldn't process the refund.

**What to do:**
1. Open the Stripe Dashboard and find the PaymentIntent (ID in the email).
2. Issue the refund manually in Stripe.
3. Email the vendor to let them know the cancellation and refund status.

### "Refund succeeded but failed to record"

**What happened:** The vendor got their money back, but the app couldn't save this fact to the database.

**What to do:**
1. **Do NOT issue another refund.** The money has already been returned.
2. The app will self-correct within a few hours (Stripe sends a follow-up notification).
3. Email the vendor to confirm the cancellation and refund.

### "UNEXPECTED: Registration auto-cancelled after Stripe Dashboard refund"

**What happened:** Someone issued a refund directly in Stripe, bypassing the app.

**What to do:**
1. Figure out who did it and why.
2. Notify the vendor (they were not emailed automatically).
3. Remind all admins: refunds should go through the app.

### "URGENT: Payment dispute filed"

**What happened:** A vendor's bank is disputing the charge (chargeback). This is time-sensitive.

**What to do:**
1. Open the Stripe Dashboard immediately (link in the email).
2. Review the dispute reason and gather evidence (registration confirmation, vendor agreement acceptance, email correspondence).
3. Respond before the deadline shown in Stripe. Missing the deadline means you automatically lose the funds.
4. If the dispute seems legitimate, consider accepting it to avoid the dispute fee.

---

## 5. Settings Changes and Timing

### Processing Fee Changes

The processing fee is calculated when a vendor loads the payment page — not at approval time. If you change the fee while vendors are actively paying:

- Vendors who haven't started paying yet will see the new fee.
- Vendors who already loaded the payment page but haven't submitted will see the new fee on refresh.
- No one gets double-charged.

**Best practice:** Make fee changes when no vendors are likely to be mid-payment — early morning or late night.

### Price Changes

Booth price changes only affect *future* approvals. Existing approved/paid registrations keep their original price. This is safe to do at any time.

### Registration Window Changes

Closing the registration window prevents new registrations but does NOT affect existing ones. Approved vendors can still pay after registration closes.

---

## 6. Security and Account Hygiene

- **Log out when you're done,** especially on shared or public computers. There is no "log out all sessions" feature — if someone gets your session, the only fix is to contact the developer.

- **Your admin session lasts 8 hours** (or 1 hour of inactivity). You'll be prompted to re-authenticate after that.

- **Don't share your OTP codes.** Each code is single-use and expires in 10 minutes.

- **Admin access is controlled by the `ADMIN_EMAILS` list.** To add or remove an admin, contact the developer.

---

## 7. First-Deployment Checklist

Since this is our first time using the app, here's what to verify during the first week:

- [ ] **Test a full cycle before going live:** Create a test registration, approve it, pay with a [Stripe test card](https://docs.stripe.com/testing#cards), then cancel and refund. Verify each email arrives.
- [ ] **Confirm webhook delivery:** Check the [Stripe Dashboard > Developers > Webhooks](https://dashboard.stripe.com/webhooks) page. You should see successful deliveries (HTTP 200) for payment events.
- [ ] **Verify email delivery:** After the first few real approvals, confirm with vendors that they received their emails. Have them check spam folders.
- [ ] **Spot-check payment amounts:** Compare the first 3-5 payments in the app against the Stripe Dashboard. Make sure booth price + processing fee matches.
- [ ] **Review the admin FAQ:** The in-app FAQ (Admin > FAQ) covers common questions about statuses, inventory, and refunds.
- [ ] **Enable admin notifications (optional):** In Settings, you can turn on email alerts for new registrations, payments, and insurance uploads. Recommended during the first week for visibility.

---

## 8. Quick Reference

| Situation | What to do |
|-----------|-----------|
| Vendor hasn't paid after a few days | Check the **Unpaid Registrations** section on the dashboard. Use the **Remind** button to send a payment reminder email. If overdue and no response, use **Revoke Approval** → "Payment deadline expired" to free the slot. |
| Need to refund a vendor | Use Cancel & Refund in the app. Never go to Stripe directly. |
| Got an alert email | Read it carefully and follow the instructions. Don't ignore it. |
| Vendor says they paid but app shows Approved | Wait a few minutes. If still stuck, check Stripe Dashboard. Contact developer if needed. |
| Vendor filed a chargeback | Respond in Stripe Dashboard before the deadline. Gather evidence from the app. |
| Two admins approving at the same time | One may see an error — just retry. No damage done. |
| Need to change a booth price | Change it in Settings. Only affects future approvals. |
| App is down or unresponsive | Vendors' data is safe (database persists). Contact developer. Do NOT issue refunds through Stripe while the app is down unless urgent. |
| Not sure what to do | Wait, check this document, or ask the developer. |

---

## Questions?

Contact the app developer at the email listed in Admin Settings (Developer Contact field). Include the registration ID and a description of the issue.
