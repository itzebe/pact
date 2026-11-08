# Final demo video — production checklist

## Status

| Item | State |
| --- | --- |
| Script | ✅ [`FINAL_VIDEO_SCRIPT.md`](FINAL_VIDEO_SCRIPT.md) |
| Storyboard | ✅ [`FINAL_VIDEO_STORYBOARD.md`](FINAL_VIDEO_STORYBOARD.md) |
| Transcript + captions | ✅ [`FINAL_VIDEO_TRANSCRIPT.md`](FINAL_VIDEO_TRANSCRIPT.md) |
| **Final narrated demo (this mission)** | ✅ [`pact-demo-final-narrated.mp4`](pact-demo-final-narrated.mp4) — **2:17**, 1600×900, H.264 + AAC, narrated (synthesized voice, `edge-tts`), burned captions, credential-free |
| Earlier narrated demo | ✅ [`../demo/pact-demo-final.mp4`](../demo/pact-demo-final.mp4) — 2:52, predates the guest experience and refunds |
| Silent walkthrough (evidence) | ✅ [`pact-walkthrough-silent.mp4`](pact-walkthrough-silent.mp4) — 0:58, no narration |
| Uploaded to YouTube (public) | ✅ <https://www.youtube.com/watch?v=CMGsmqTr6FQ> — "PACT — Agreements You Can Verify \| PayPal AI Hackathon 2026", 2:17 |
| Link added to the Devpost form | ⏳ Owner action (the video is already embedded on the public Devpost page; confirming the form field is an owner action) |

### The final narrated demo

`pact-demo-final-narrated.mp4` is the final competition cut: **2:17**, under the
three-minute limit. It is a genuine screen recording of the live production app at
<https://pact-flame.vercel.app> against PayPal **Sandbox**, with:

- **Narration.** A synthesized voice (`edge-tts`, `en-US-GuyNeural`) — the same pipeline
  the project's earlier demo used. The voice is machine-generated, not a human recording;
  it is described that way everywhere. An owner may replace it with their own voice.
- **Burned captions.** Concise, one line-group per beat, for muted playback.
- **A persistent label.** "PAYPAL SANDBOX · TEST ENVIRONMENT" sits in the top-right of
  every frame.
- **Credential-free.** The PayPal Sandbox approval runs in a separate, *unrecorded*
  browser context, so the PayPal login page never appears; the PACT buyer hint is
  redacted in the DOM on appearance. An OCR scan over 256 sampled frames (2 fps) found
  no occurrence of the buyer email or password.

Beats: title → guest workspace → job + negotiation → approval → real PayPal Sandbox
approval → captured/settled → hash-chained ledger → refunded → closing card.

**Rebuild it:**

```bash
python3 docs/judge-demo/tools/record_final.py     # records footage + beat times
python3 docs/judge-demo/tools/assemble_final.py   # narration, captions, export
```

Both scripts are committed under `docs/judge-demo/tools/`.

> **Accuracy note.** The narration is synthesized, not human. The video is never
> described as human-narrated, and the app is never described as moving real money.

## Pre-record — completed ✅

- [x] Production confirmed READY and PayPal in **Sandbox** (`GET /api/health`).
- [x] Guest flow confirmed (open the URL, a workspace appears).
- [x] Recorded at **1600×900**, 30 fps, browser zoom 100%.
- [x] Headless capture; no personal tabs/notifications.
- [x] PayPal approval ran in a **separate, unrecorded** browser context.
- [x] Sandbox buyer email/password redacted in the DOM before they could appear.

## During record — completed ✅

- [x] One continuous take; the same job across Beats 3–6.
- [x] The PayPal step used `www.sandbox.paypal.com` (not `paypal.com`) — outside the recording.
- [x] Sandbox buyer credentials never appear (separate context + DOM redaction).
- [x] The run ended **settled** and then **refunded** (no voided take).

## Post-record — completed ✅

- [x] Total runtime **2:17** — under 3:00.
- [x] Captions burned in (condensed lines, listed in the transcript).
- [x] No third-party trademarks or copyrighted music.
- [x] On-screen capture/settlement state is the one the visible job produced.
- [x] Exported MP4 (H.264, AAC), 1600×900, 6.3 MB, `+faststart`.

## Upload — done ✅

The final cut is **public on YouTube** and embedded on the Devpost page:

- [x] Uploaded to YouTube, visibility **Public**:
      <https://www.youtube.com/watch?v=CMGsmqTr6FQ>
- [x] Actual title on YouTube: `PACT — Agreements You Can Verify | PayPal AI Hackathon 2026`
- [x] Duration **2:17** (under 3:00); verified public via the YouTube oEmbed API.
- [x] Embedded on <https://devpost.com/software/pact-verified-payments-for-ai-agents>.

## Owner-only actions

1. Optionally re-record the narration in your own voice (the current cut uses synthesized
   narration). The script and timings are in this folder.
2. Confirm the video URL is present in the Devpost submission **form** field (the public
   page already embeds it).
3. Click **Submit** on Devpost (explicit owner authorization required).

## Reproducing the final recording

The final cut is built by two committed scripts:

```bash
pip install edge-tts imageio-ffmpeg      # narration + encoder
python3 docs/judge-demo/tools/record_final.py     # footage + beat times -> /tmp/final_rec
python3 docs/judge-demo/tools/assemble_final.py   # narration + captions -> docs/judge-demo/
```

`record_final.py` records the live app (headless Chromium) and writes the wall-clock start
of each beat to `/tmp/final_rec/beats.json`; `assemble_final.py` retimes each beat to its
narration, muxes the `edge-tts` voice, burns captions and the Sandbox label, and exports
the MP4. The project's earlier harness remains at
[`../demo/tools/record_demo.py`](../demo/tools/record_demo.py).
