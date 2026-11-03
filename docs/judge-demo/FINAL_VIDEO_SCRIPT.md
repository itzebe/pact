# Final demo video — script

**Target length:** 100 seconds (hard limit: under 180 seconds; judges are not required
to watch beyond three minutes).
**Source of footage:** the live production product, <https://pact-flame.vercel.app>, in
the PayPal **Sandbox** environment.
**Voice:** calm, specific, no marketing adjectives.

Each beat below is one continuous shot of the real application in a named state. The
narration says only what is on screen at that moment. Read time assumes ~2.5 words/second.

---

## Beat 1 — The tension · 0:00–0:10

**On screen:** title card, then the PACT header fading in.
**Shot:** static, no cursor movement.

> Two strangers agree to do a job. One is afraid to pay and get nothing. The other is
> afraid to deliver and never get paid. Neither wants to open another account just to
> do one transaction.

---

## Beat 2 — The product opens · 0:10–0:25

**On screen:** the production URL loads. The header reads **Guest workspace** and
**PayPal Sandbox**. The composer is visible on the right; the settlement view is empty.
**Shot:** one page load, cursor settles on the composer.

> This is Pact. It opens straight into a working guest workspace — no sign-up, no form,
> no email. This is running against the real PayPal Sandbox, not a mock. And the
> workspace is mine alone: another guest cannot see it.

---

## Beat 3 — Coordination · 0:25–0:45

**On screen:** **Post a job** panel with the *Launch copy* preset applied; the brief,
acceptance criteria and budget ceiling filled in. Then **Post job and settle** is
pressed and the **Negotiation** tab fills with rounds.
**Shot:** cursor types nothing new (fields already filled); one click on **Post job and
settle**; the negotiation transcript scrolls once.

> I post a job with a brief, acceptance criteria, and a budget ceiling of two hundred
> dollars. Seller agents quote, the buyer counters, and the price converges. Every
> round is recorded, and the budget is enforced in code — an agreement above the ceiling
> is rejected by the engine, not left to the model.

---

## Beat 4 — Payment · 0:45–1:10

**On screen:** the payment panel in the `awaiting_approval` state —
**"Waiting for your approval in PayPal"** — with **Open PayPal approval** and the
guest-only Sandbox test-buyer note. The link is opened; the real
`www.sandbox.paypal.com` checkout appears; the purchase is approved; the browser
returns to PACT; **I approved — continue** is pressed.
**Shot:** click through to PayPal, complete the approval, return. Buyer credentials are
blurred.

> Funds are held before any work starts. PayPal requires the buyer to approve the order,
> so Pact stops here and hands me the approval link instead of pretending the order is
> already a hold. I approve it in the real PayPal Sandbox — and this is Sandbox, so no
> real money moves. Back in Pact, I continue, and it authorizes the hold.

---

## Beat 5 — Proof · 1:10–1:30

**On screen:** the panel now reads **"Paid — captured from escrow"**; the summary shows
**Verification: Passed, score 1.00**; the **Ledger** tab shows the hash-chained entries.
**Shot:** cursor moves to **Ledger**; the grid scrolls once.

> The delivery is checked against every acceptance criterion by an independent verifier,
> and only a passing verdict releases the money. The capture is confirmed by PayPal, and
> every step — order, approval, hold, verification, release — is in a hash-chained
> ledger that can be re-verified.

---

## Beat 6 — Protection · 1:30–1:45

**On screen:** **Refund this payment** is pressed, confirmed, and the panel becomes
**"Payment refunded"**.
**Shot:** two clicks (refund, confirm), then the state change.

> If I need to reverse it, the refund runs through PayPal too — capped at the captured
> amount and deduplicated, so asking twice never moves money twice. A failed verification
> would have voided the hold instead, and the buyer would be charged nothing.

---

## Beat 7 — Payoff · 1:45–2:00

**On screen:** the settled-then-refunded job with the ledger behind it; then a closing
card with the live URL and the repository.
**Shot:** slow pull-back to the full window; cut to closing card.

> Pact doesn't ask strangers to trust each other blindly. It gives their agreement a
> payment lifecycle they can verify — a real hold, a real capture, a real refund, and a
> record of all three.

---

## Production notes

- **No fabricated state.** Every panel shown is a real state of the deployed app. If a
  run ends `voided` (the verifier is model-backed and non-deterministic), re-record that
  take rather than narrating a settlement that did not happen.
- **Sandbox only.** Never say or imply real money moved. The words "Sandbox" and "no real
  money" appear on screen and in the narration.
- **Credentials.** The Sandbox test-buyer email and password must be blurred in every
  frame they appear.
- **Timing.** If the read runs long, cut adjectives from Beat 3 and Beat 5 first; never
  cut Beat 4 or Beat 6, which carry the proof.
