# Evidence segments in the demo video

## What the three supplied captures are

The three inputs are not the same kind of artefact. Two are PACT application
screenshots; one is a genuine PayPal Sandbox receipt.

| | PayPal record | PACT capture A | PACT capture B |
|---|---|---|---|
| File | `pact-paypal-receipt.png` | `pact-capture-negotiation.png` | `pact-capture-outcome.png` |
| Origin | PayPal Sandbox (`sandbox.paypal.com`) | `pact-flame.vercel.app` | `pact-flame.vercel.app` |
| Size | 720x1612 | 720x1612 | 720x1612 |
| Content | $125.00 payment, Oct 25 2026, seller *Test Store*, transaction `1T8850007E538313C`, order summary "Pact escrow" / "Landing page copy for a fintech launch" | negotiation: Atlas ask USD 140.00, buyer counter USD 120.00 | settled job: $125.00 agreed, $200.00 ceiling, verification Passed score 1.00, $125.00 released |

Only the first image is PayPal's own record. The other two contain no PayPal
order id, authorization id, capture id, or PayPal-branded payment screen — the
only "PayPal" text on them is PACT's header badge and step bar. They are
presented as **PACT's own application records**, and the narration says so.

### What the receipt does and does not prove

The receipt is a PayPal **Sandbox** payment: no real money moved. It shows a
$125.00 payment to a test seller, on the same date the PACT captures were taken,
for a brief that matches the PACT job ("Landing page copy for a fintech launch",
$125.00).

It does **not** prove that PACT created that payment. The receipt carries a
PayPal transaction id, while PACT's own record carries the order, authorization
and capture ids of the order PACT drove; the images share no identifier. So the
receipt and the PACT record are reported as **consistent** — same amount, same
Sandbox environment, same brief — not as the same transaction. The narration
draws that line explicitly ("Different records, different purposes").

### Redaction

The receipt carries a cardholder name, a masked primary card, a backup card, and
seller contact details. None of that is evidence, so `redact_receipt.py` blurs
it. The evidence values are deliberately left intact: amount, date, transaction
id, seller name, and order summary.

## Where the segments sit

The submission cut is `pact-demo-final.mp4` (2:52, under the three-minute cap).
The original video is unchanged and is only read; two segments are spliced in at
measured cuts of the original:

| time | content |
|---|---|
| 0:00 – 1:15 | original demo, unaltered (ends with the PayPal Sandbox checkout) |
| 1:15 – 1:33 | **Segment A** — PayPal Sandbox receipt, then PACT's settlement record |
| 1:33 – 2:24 | original demo, unaltered (verification, then the hash-chained ledger) |
| 2:24 – 2:42 | **Segment B** — PACT's negotiation capture, then the settled-outcome capture |
| 2:42 – 2:52 | original closing shot, unaltered |

Segment A lands right after the payment section (cut at t=74.8s in this cut), so
the payment evidence sits with the payment narrative. Segment B lands after the
ledger section (cut at t=124.7s), before the closing shot.

`pact-demo-enhanced.mp4` is the same edit before pause compression (3:06), with
its segments at 1:21 and 2:36. `pact-demo-final.mp4` shortens the ~1s of silence
at each sentence end to 0.65s; no spoken word, scene or card is removed, so the
final cut is simply the enhanced cut with the dead air tightened.

## Reproducing it

```bash
python docs/demo/tools/redact_receipt.py .evidence-work/s3.png .evidence-work/s3_redacted.png
python docs/demo/tools/evidence_cards.py .evidence-work .evidence-work/build
python docs/demo/tools/assemble_evidence.py .evidence-work/build .evidence-work/audio .evidence-work/render
python docs/demo/tools/trim_pauses.py docs/demo/pact-demo-enhanced.mp4 docs/demo/pact-demo-final.mp4 0.65
python docs/demo/tools/verify_evidence.py .evidence-work docs/demo/pact-demo-final.mp4
```

`evidence_cards.py` crops and scales the original pixels (nothing is
redrawn); `assemble_evidence.py` splices the cards into a copy of the original;
`trim_pauses.py` shortens the silent gaps in both audio and video at the same
time intervals, so they stay in sync. Narration text lives in
`evidence_narration.json`. Requires `edge-tts`, `imageio-ffmpeg` and `Pillow`;
verification also uses `tesseract`.

## Verification

`verify_evidence.py` checks, against the rendered file: each screenshot is
present pixel-for-pixel in its card; every value visible in the originals is
legible in the render; the receipt's payment-method details stay redacted; the
Sandbox evidence is labelled as Sandbox; no black or silent stretches were
introduced; `pact-demo.mp4` is byte-unchanged; and the submission cut is under
180 seconds. Sample times are written against the enhanced cut and mapped onto
the final cut automatically, since the final cut only removes silence.
