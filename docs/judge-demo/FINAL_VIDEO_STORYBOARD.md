# Final demo video — storyboard

Frame-by-frame plan for the 100-second demo. Each row is one shot. "State" is the real
application state that must be on screen for the shot to be honest.

Resolution 1600×900 (16:9), browser zoom 100%. Record at 30 fps. Desktop framing; a
short mobile insert is optional in Beat 2 if time allows.

---

## Beat 1 — The tension (0:00–0:10)

| | |
| --- | --- |
| Visual | Title card: **"Two strangers. One agreement. Two fears."** on the PACT ink background. Fade to the PACT header. |
| Motion | Static. No cursor. |
| Audio | Narration (Beat 1). Quiet bed, no music with third-party rights. |
| State | N/A (card) |

## Beat 2 — The product opens (0:10–0:25)

| | |
| --- | --- |
| Visual | `https://pact-flame.vercel.app` loads. Header shows **Guest workspace**, **New workspace**, **PayPal Sandbox**. Empty settlement panel centre; **Post a job** panel right. |
| Motion | Page load; cursor drifts to the composer. Optional 2-second mobile insert at 390 px. |
| Audio | Narration (Beat 2). |
| State | Fresh guest workspace, no jobs. |

## Beat 3 — Coordination (0:25–0:45)

| | |
| --- | --- |
| Visual | **Post a job** panel with *Launch copy* preset; **Deliverable**, **Brief**, **Acceptance criteria**, **Budget ceiling = 200**. Then the **Negotiation** tab: rounds with seller ASK, buyer counter, and the accepted offer. |
| Motion | One click on **Post job and settle**; transcript scrolls once. |
| Audio | Narration (Beat 3). |
| State | Job created; negotiation converged to an agreed price. |

## Beat 4 — Payment (0:45–1:10)

| | |
| --- | --- |
| Visual | Payment panel: **"Waiting for your approval in PayPal"**, **Open PayPal approval**, guest-only Sandbox test-buyer note (blurred). Real `www.sandbox.paypal.com` checkout. Return to PACT. **I approved — continue**. |
| Motion | Click **Open PayPal approval** (new tab), complete approval, return, click **I approved — continue**. |
| Audio | Narration (Beat 4). |
| State | `awaiting_approval` → hold placed (`held`). |
| Safety | Blur the Sandbox buyer email and password in every frame. |

## Beat 5 — Proof (1:10–1:30)

| | |
| --- | --- |
| Visual | Panel: **"Paid — captured from escrow"**. Summary: **Verification Passed, score 1.00**. Then the **Ledger** tab: hash-chained entries, newest first. |
| Motion | Cursor moves to **Ledger**; the grid scrolls once. |
| Audio | Narration (Beat 5). |
| State | `released` (capture `COMPLETED`); ledger chain valid. |

## Beat 6 — Protection (1:30–1:45)

| | |
| --- | --- |
| Visual | **Refund this payment** → confirmation → **"Payment refunded"**. |
| Motion | Two clicks, then the state change settles. |
| Audio | Narration (Beat 6). |
| State | `refunded`. |

## Beat 7 — Payoff (1:45–2:00)

| | |
| --- | --- |
| Visual | The job with the ledger behind it, then a closing card: **Pact** — live URL and repository. |
| Motion | Slow pull-back, cut to card. |
| Audio | Narration (Beat 7). |
| State | Terminal, stable. |

---

## Continuity rules

- The same job is followed across Beats 3–6, so the ledger in Beat 5 belongs to the
  payment in Beat 4 and the refund in Beat 6.
- If the run ends `voided`, restart the take — do not splice a different job's ledger.
- Every on-screen identifier (order / authorization / capture / refund) must be the one
  the visible job actually produced.
