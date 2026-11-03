# Final demo video — transcript

This is the exact narration text for the demo, with the on-screen state each line is
spoken over. It is a **script**, not a transcript of an existing recording — no video
has been recorded yet. See [`FINAL_VIDEO_CHECKLIST.md`](FINAL_VIDEO_CHECKLIST.md).

Word count of the narration: ~330 words → ~100 seconds at ~2.5 words/second.

---

**[0:00 — title card]**

> Two strangers agree to do a job. One is afraid to pay and get nothing. The other is
> afraid to deliver and never get paid. Neither wants to open another account just to do
> one transaction.

**[0:10 — guest workspace loads]**

> This is Pact. It opens straight into a working guest workspace — no sign-up, no form,
> no email. This is running against the real PayPal Sandbox, not a mock. And the
> workspace is mine alone: another guest cannot see it.

**[0:25 — job composer → negotiation]**

> I post a job with a brief, acceptance criteria, and a budget ceiling of two hundred
> dollars. Seller agents quote, the buyer counters, and the price converges. Every round
> is recorded, and the budget is enforced in code — an agreement above the ceiling is
> rejected by the engine, not left to the model.

**[0:45 — approval panel → PayPal Sandbox → return]**

> Funds are held before any work starts. PayPal requires the buyer to approve the order,
> so Pact stops here and hands me the approval link instead of pretending the order is
> already a hold. I approve it in the real PayPal Sandbox — and this is Sandbox, so no
> real money moves. Back in Pact, I continue, and it authorizes the hold.

**[1:10 — settled panel → verification → ledger]**

> The delivery is checked against every acceptance criterion by an independent verifier,
> and only a passing verdict releases the money. The capture is confirmed by PayPal, and
> every step — order, approval, hold, verification, release — is in a hash-chained
> ledger that can be re-verified.

**[1:30 — refund]**

> If I need to reverse it, the refund runs through PayPal too — capped at the captured
> amount and deduplicated, so asking twice never moves money twice. A failed verification
> would have voided the hold instead, and the buyer would be charged nothing.

**[1:45 — closing card]**

> Pact doesn't ask strangers to trust each other blindly. It gives their agreement a
> payment lifecycle they can verify — a real hold, a real capture, a real refund, and a
> record of all three.

---

## Captions

Burned-in captions should mirror the narration above verbatim. Keep them on two lines
maximum, bottom-centred, high contrast. If a caption would cover the payment panel,
place it at the top for Beats 4 and 5.

## What the transcript deliberately does not say

- It never claims real money moved, and never uses "live" or "production payment" as a
  description of this environment. (It does say "no real money moves", to make the Sandbox
  boundary explicit.)
- It never claims uniqueness ("the only", "the first").
- It never narrates a state that is not on screen at that moment.
