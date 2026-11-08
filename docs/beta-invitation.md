# PACT beta — tester invitation and onboarding

**Prepared for review. Not sent.** Send this to an invited tester only after the operator
sets the reporting channel in [`beta-launch-checklist.md`](beta-launch-checklist.md) §6.
No accounts are created on a tester's behalf; no public announcement is made.

---

## What PACT is

PACT is a guest-first work coordination and payment coordination platform. A buyer and a
seller agent agree a price for a piece of work; PACT holds the buyer's funds in escrow
through PayPal until an independent verifier confirms the work is delivered; only then does
the money move. If the work is not delivered, the funds are refunded.

## What you are testing

You are testing the **end-to-end Sandbox flow** — creating work, agreeing a price, moving
money in and out of escrow, and seeing the result in the ledger. Your goal is to find
anything that looks wrong, stuck, or confusing.

**Payments are simulated through PayPal Sandbox. No real money moves. You will not pay
anything and you cannot be charged.**

## Before you start

- Use a desktop or laptop browser if you can (the ledger is a data grid).
- You do **not** need an account. Opening the app creates a private guest workspace for you.
- Have the reporting channel the operator gave you open, so you can report as you go.

## The test journeys

Do these in order. At each step, check that what you see matches the expected result. If it
does not, stop and report it — do not try to "fix" it.

1. **Create a job.** Open <https://pact-flame.vercel.app/>. You land in a private guest
   workspace. Create a job with a short brief and acceptance criteria. → The job appears in
   your workspace.
2. **Submit and review offers.** Let the seller agents offer on the job. → Offers are listed.
3. **Create and review an agreement.** Accept an offer. → An agreed price is recorded.
4. **Initiate payment authorization.** Start the escrow run. → The job stops at
   **awaiting approval** and shows a PayPal approval step.
5. **Complete Sandbox buyer approval.** The app shows a **sandbox buyer** email and password.
   Use those (not a real PayPal account) to approve. → The funds are **held**, not captured.
6. **Capture payment.** Let the verifier check the delivery. → On a passing verdict the
   payment is **captured** and the job is released.
7. **Verify settlement.** Open the **Ledger** tab. → The ledger verifies and shows the
   capture as a settlement entry.
8. **Execute a refund.** Refund the payment (full or partial). → A refund is recorded.
9. **Verify refund completion.** Re-open the ledger/job. → The refund shows as completed.
10. **Review transaction history and status.** Look over the ledger and the job status
    fields. → Statuses are consistent; nothing is shown as succeeded unless its status says
    so.

If a second tester is available, also try to open their job's URL from your workspace — you
should get a **404**, not their data.

## How to report a problem

Report **every** problem, even small ones, through the channel the operator gave you:

- **Say what you did, what you expected, and what happened.** Include the approximate time
  (UTC) and the job id if you have one.
- **Do not** post the sandbox buyer password, any real credential, real financial
  information, personal data, or a full transaction id in any public place.
- A payment problem (a capture or refund that looks wrong, or a job stuck) is urgent — mark
  it as urgent so the operator sees it first.

## What not to submit

- Real payment or financial details of any kind.
- Real PayPal account credentials.
- Personal data you do not want shared.
- Anything that looks like a secret or a key.

## Contact

Use the reporting channel the operator provides:
`<channel to be set by the operator>`.

Thank you for helping test PACT. Your reports directly shape what ships.

---

*This invitation is prepared for operator review and has not been sent to anyone.*
