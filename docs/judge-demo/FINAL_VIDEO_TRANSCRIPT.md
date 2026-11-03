# Final demo video — transcript

The exact narration of the final cut,
[`pact-demo-final-narrated.mp4`](pact-demo-final-narrated.mp4) (**2:17**), with the
on-screen state each line is spoken over.

The narration is **synthesized** (`edge-tts`, `en-US-GuyNeural`) — the same pipeline the
project's earlier demo used. It is machine-generated, not a human recording. An owner may
replace the voice with their own; the timing below is what the current cut uses.

Captions burned into the video are condensed versions of these lines, not verbatim.

---

**[0:00 — title card]** · *"Two strangers. One agreement. Two fears."*

> Two strangers agree to do a job. One is afraid to pay and get nothing. The other is
> afraid to deliver and never get paid. Neither wants to open another account just to do
> one transaction.

**[0:18 — guest workspace loads]**

> This is Pact. It opens straight into a working guest workspace — no sign-up, no form,
> no email. Everything here is real: a live model, a durable database, and the actual
> PayPal Sandbox, not a mock. And the workspace is mine alone: another guest cannot see it.

**[0:38 — job composer → negotiation]**

> I post a job with a brief, acceptance criteria, and a budget ceiling of two hundred
> dollars. Seller agents quote, the buyer counters, and the price converges. Every round
> is recorded, and the budget is enforced in code — an agreement above the ceiling is
> rejected by the engine, not left to the model. Sellers are ranked by verified-delivery
> reputation, so an unreliable seller has to be cheaper to win.

**[1:04 — approval panel → PayPal Sandbox → return]**

> Funds are held before any work starts. PayPal requires the buyer to approve the order,
> so Pact stops here and hands me the approval link instead of pretending the order is
> already a hold. I approve it in the real PayPal Sandbox — and this is Sandbox, so no
> real money moves. Back in Pact, I continue, and it authorizes the hold.

**[1:27 — settled panel → verification → ledger]**

> The delivery is checked against every acceptance criterion by an independent verifier,
> and only a passing verdict releases the money. The capture is confirmed by PayPal, and
> every step — order, approval, hold, verification, release — is in a hash-chained
> ledger that can be re-verified.

**[1:45 — refund]**

> If I need to reverse it, the refund runs through PayPal too — capped at the captured
> amount and deduplicated, so asking twice never moves money twice. A failed verification
> would have voided the hold instead, and the buyer would be charged nothing.

**[2:00 — closing card]**

> Pact doesn't ask strangers to trust each other blindly. It gives their agreement a
> payment lifecycle they can verify — a real hold, a real capture, a real refund, and a
> record of all three.

---

## On-screen captions (as burned in)

| Beat | Captions |
| --- | --- |
| 1 | Two strangers agree to do a job. · One fears paying and getting nothing. · The other fears delivering and never being paid. · Neither wants another account. |
| 2 | This is Pact — it opens into a guest workspace. · No sign-up, no form, no email. · A live model, a durable database, the real PayPal Sandbox. · And the workspace is mine alone. |
| 3 | I post a job: brief, acceptance criteria, a $200 ceiling. · Seller agents quote; the buyer counters; the price converges. · Every round is recorded — the budget is enforced in code. · Sellers are ranked by verified-delivery reputation. |
| 4 | Funds are held before work starts. · PayPal requires the buyer to approve — so Pact stops and hands over the link. · I approve in the real PayPal Sandbox. No real money moves. · Back in Pact, it authorizes the hold. |
| 5 | An independent verifier checks every acceptance criterion. · Only a passing verdict releases the money. · PayPal confirms the capture. · Every step is in a hash-chained ledger you can re-verify. |
| 6 | Need to reverse it? The refund runs through PayPal too. · Capped at the captured amount and deduplicated. · A failed verification would have voided the hold instead. · The buyer would be charged nothing. |
| 7 | Pact doesn't ask strangers to trust each other blindly. · It gives their agreement a payment lifecycle they can verify. · A real hold, a real capture, a real refund — and a record of all three. |

## Persistent on-screen label

Every frame carries **"PAYPAL SANDBOX · TEST ENVIRONMENT"** in the top-right corner.

## What the transcript deliberately does not say

- It never claims real money moved, and never uses "live" or "production payment" as a
  description of this environment. (It does say "no real money moves", to make the Sandbox
  boundary explicit.)
- It never claims uniqueness ("the only", "the first").
- It never narrates a state that is not on screen at that moment.
- It never claims the narration is human.
