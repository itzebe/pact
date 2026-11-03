# Screenshot index

Every image here was captured directly from the **production** application at
<https://pact-flame.vercel.app> with browser automation (headless Chromium via
Playwright), driving a genuine guest workspace through the real PayPal **Sandbox**
flow. None are illustrations, mockups, or recreated interfaces.

- Desktop viewport: **1440 × 1000** CSS px, captured at `deviceScaleFactor=2`
  (so the PNG is 2880 × 2000).
- Mobile viewport: **390 × 844** CSS px, captured at `deviceScaleFactor=3`
  (so the PNG is 1170 × 2532).
- No horizontal overflow on mobile (measured: `scrollWidth − clientWidth = 0`).
- The Sandbox buyer credentials are redacted in every image that would otherwise
  show them; an OCR check confirms no credential value appears in any file.

| File | Viewport | State | Demonstrates | From production |
| --- | --- | --- | --- | --- |
| `01-guest-workspace.png` | 1440 × 1000 | First screen, fresh guest | The working product is the first screen: composer, settlement view, and the header reading **Guest workspace** / **PayPal Sandbox**. No credential prompt. | Yes |
| `02-job-composer.png` | 1440 × 1000 | Composer with a preset applied | Job creation: **Deliverable**, **Brief**, **Acceptance criteria**, **Budget ceiling**, and the **Post job and settle** action. | Yes |
| `03-job-details.png` | 1440 × 1000 | Job in the hold stage | Job header, brief, the six-step bar (Negotiate → Agree → Hold escrow → Deliver → Verify → Settle), and the coordination summary (agreed price, seller, verification, held/returned). | Yes |
| `04-offer-negotiation.png` | element capture (≈998 × 621) | Negotiation transcript | The round-by-round offers and counter-offers, with the budget ceiling shown per round and the accepted offer marked. | Yes |
| `05-payment-approval.png` | 1440 × 1000 | `awaiting_approval` | The buyer-facing payment panel: **Waiting for your approval in PayPal**, the **Open PayPal approval** link, and the guest-only Sandbox test-buyer panel. **Credentials redacted.** | Yes |
| `06-payment-confirmation.png` | 1440 × 1000 | `released` (settled) | **Paid — captured from escrow**, with the verification result (Passed, score 1.00) and the **Refund this payment** control. | Yes |
| `07-settlement-ledger.png` | 1440 × 1000 | Settled job, Ledger tab | The hash-chained ledger: every step from job creation to settlement, in order. | Yes |
| `08-refund-state.png` | 1440 × 1000 | `refunded` | **Payment refunded** after a real Sandbox refund, with the negotiation transcript still visible. | Yes |
| `09-mobile-workspace.png` | 390 × 844 | First screen, fresh guest (mobile) | The working product on a phone: header, settlement view, and composer reachable without horizontal scroll. | Yes |
| `10-mobile-job-flow.png` | 390 × 844 | Composer with a job (mobile) | Job creation on a phone: the composer fields render cleanly at 390 px with no overflow. | Yes |

## What each capture proves — and what it does not

- The captures are of the **running production deployment**, at commit `46fa3b5e`
  (`dpl_CqWUFB6wnotWgD8MwQGVRf7nwpEY`).
- The settled state in `06`, `07` and `08` was produced by a **real** Sandbox run
  during this capture session: a real order was created, approved in the PayPal
  Sandbox checkout, authorized, captured, and then refunded. The identifiers for
  the specific journey in the original verification report are preserved in
  [`../PRESERVED_VERIFICATION_EVIDENCE.md`](../PRESERVED_VERIFICATION_EVIDENCE.md).
- These images demonstrate the product's behaviour. They are **not** PayPal
  receipts, and they carry no PayPal-branded payment screen. The PayPal-side
  evidence is the provider identifier set recorded in the evidence document.

## Reproducing

The capture used Playwright against the public URL with a guest workspace, and
redacted the Sandbox buyer panel in the DOM before any screenshot was taken. See
[`../JUDGE_WALKTHROUGH.md`](../JUDGE_WALKTHROUGH.md) for the same flow a human
judge follows.
