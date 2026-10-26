# Evidence segment in `pact-demo-enhanced.mp4`

## What the two supplied captures actually are

Both inputs are **PACT application screenshots**, not PayPal payment screenshots.

| | capture 1 | capture 2 |
|---|---|---|
| File | `pact-capture-outcome.png` | `pact-capture-negotiation.png` |
| Size | 720x1612 (phone portrait) | 720x1612 |
| Source | Android device screenshot of `pact-flame.vercel.app` | same |
| Device clock | 17:41 | 17:42 |
| Content | settled job: $125.00 agreed, $200.00 ceiling, verification Passed score 1.00, $125.00 released to seller | negotiation: Atlas ask USD 140.00, buyer counter USD 120.00, plus nova/quill asks |

Neither image contains a PayPal order id, authorization id, capture id, receipt,
transaction reference, or a PayPal-branded payment screen. The only "PayPal"
text is the PACT header badge reading *Pact PayPal Sandbox* and the step bar.

They are therefore presented as **PACT's own application records**, and the
narration says so explicitly ("These are PACT's own records, not PayPal
receipts"). No claim is made that they evidence a PayPal settlement, an escrow
release, or hash-chain integrity.

The two captures share the settled job's $200 ceiling and seller Atlas, so
capture 2 is described as *consistent with* capture 1 — not asserted to be the
same transaction, because the screenshots carry no job id.

## Where the segment sits

The original video is unchanged and is only read. The evidence segment is
spliced at the measured ledger→closing boundary of the original (t=133.0s), so
it lands after the settlement/verification narrative and before the closing shot.

| time | content |
|---|---|
| 0:00 – 2:13 | original demo, unaltered |
| 2:13 – 2:16 | Evidence 1: settled job outcome and escrow release |
| 2:16 – 2:24 | Evidence 2: negotiation record |
| 2:24 – 2:34 | PACT's settlement record (application record, not a receipt) |
| 2:34 – 2:57 | original closing shot, unaltered |

## Reproducing it

```bash
python docs/demo/tools/evidence_cards.py docs/demo/evidence-cards
python docs/demo/tools/assemble_enhanced.py
```

`evidence_cards.py` crops and scales the original pixels (nothing is redrawn) and
`assemble_enhanced.py` splices the cards into a copy of the original. Requires
`edge-tts`, `imageio-ffmpeg` and `Pillow`.
