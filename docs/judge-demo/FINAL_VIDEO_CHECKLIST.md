# Final demo video — production checklist

## Status

| Item | State |
| --- | --- |
| Script | ✅ [`FINAL_VIDEO_SCRIPT.md`](FINAL_VIDEO_SCRIPT.md) |
| Storyboard | ✅ [`FINAL_VIDEO_STORYBOARD.md`](FINAL_VIDEO_STORYBOARD.md) |
| Transcript + captions | ✅ [`FINAL_VIDEO_TRANSCRIPT.md`](FINAL_VIDEO_TRANSCRIPT.md) |
| **Existing narrated demo (earlier cut)** | ✅ [`../demo/pact-demo-final.mp4`](../demo/pact-demo-final.mp4) — 2:52, narrated, live app + PayPal Sandbox. Predates the guest experience and refunds. |
| **New silent screen walkthrough** | ✅ [`pact-walkthrough-silent.mp4`](pact-walkthrough-silent.mp4) — 58 s, 1600×900, H.264, credential-free; shows the guest flow and a refund |
| Final narrated cut (this script) | ⏳ Owner action — re-narrate the new walkthrough (or re-record) so the video shows the guest flow and refund |
| Uploaded to YouTube (public) | ⏳ Owner action (needs the owner's channel) |
| Link added to the Devpost form | ⏳ Owner action (submitting to Devpost requires owner approval) |

### What exists today, precisely

Two genuine recordings exist, and they are different cuts:

1. **`../demo/pact-demo-final.mp4`** — a **narrated** 2:52 screen capture of the live app
   against PayPal Sandbox (title → problem → agreement → payment → verifier → ledger →
   close). It is honest and complete for the settlement story, but it was produced
   *before* the guest experience and the refund flow existed, so it does not show either.
2. **`pact-walkthrough-silent.mp4`** (this folder) — a **silent** 58 s walkthrough of the
   current product: guest workspace → job composer → negotiation → approval panel → real
   PayPal Sandbox approval → captured/settled → hash-chained ledger → refunded.

**For the hackathon, the strongest submission is a new narrated cut** that follows
[`FINAL_VIDEO_SCRIPT.md`](FINAL_VIDEO_SCRIPT.md) and combines both: the existing video's
narration quality with the new walkthrough's coverage of the guest flow and refund. That
re-narration/recording is an owner action.

### The new silent walkthrough

`pact-walkthrough-silent.mp4` is a **genuine, silent screen recording** of the live
production app at <https://pact-flame.vercel.app>, captured with headless Chromium
(Playwright video). It follows the demo beats listed above.

- **Silent.** There is no narration track. It is a silent walkthrough, not a narrated
  demo — do not describe it otherwise.
- **Credential-free.** The PayPal Sandbox approval runs in a separate, *unrecorded*
  browser context, so the PayPal login page never appears. The PACT approval panel's
  Sandbox buyer hint is redacted in the DOM the moment it appears. An OCR check over
  117 sampled frames (2 fps) found **no occurrence** of the buyer email or password.
- **Runtime 0:58** — well under the 3-minute limit, but shorter than the 1:40 target
  because it carries no narration.

> **Accuracy note.** The new artifact is a silent walkthrough and is never described as a
> narrated video. The existing `pact-demo-final.mp4` *is* narrated; it is described as an
> earlier cut that predates the guest flow and refunds.

## Pre-record

- [ ] Confirm production is READY and PayPal is in **Sandbox** (`GET /api/health`).
- [ ] Confirm the guest flow works (open the URL, a workspace appears).
- [ ] Record at **1600×900**, 30 fps, browser zoom 100%.
- [ ] Close other tabs/notifications; hide the bookmarks bar.
- [ ] Sign out of any personal PayPal session; use only the Sandbox test buyer.
- [ ] Have the redaction ready for the Sandbox buyer email/password.

## During record

- [ ] One continuous take per beat; keep the same job across Beats 3–6.
- [ ] The PayPal step shows `www.sandbox.paypal.com` (not `paypal.com`).
- [ ] Blur the Sandbox buyer credentials in every frame they appear.
- [ ] No dead screen time — cut waiting on the approval return.
- [ ] If the run ends `voided`, restart the take (do not narrate a settlement that did
      not happen).

## Post-record

- [ ] Total runtime **under 3:00** (target 1:40).
- [ ] Burned-in captions match the transcript verbatim.
- [ ] No third-party trademarks or copyrighted music.
- [ ] Verify the on-screen capture id is the one the visible job produced.
- [ ] Export MP4 (H.264, AAC), ≤ 1080p.

## Upload

- [ ] Upload to YouTube, visibility **Public**.
- [ ] Title: `Pact — verifiable escrow for agent work (PayPal Sandbox demo)`.
- [ ] Add the YouTube link to the Devpost submission form.
- [ ] Confirm the link resolves in an incognito window.

## Owner-only actions

1. Record the narration (or approve a TTS cut) — the agent can supply a silent walkthrough
   but cannot publish audio/video on the owner's behalf.
2. Upload to the owner's YouTube channel.
3. Paste the link into Devpost.
4. Click **Submit** on Devpost (explicit owner authorization required).

## Reproducing the recording environment

The repository already ships a recording harness at
[`../demo/tools/record_demo.py`](../demo/tools/record_demo.py) (Playwright video capture
with TTS narration via `edge_tts` and encoding via `imageio_ffmpeg`). The narrated cut
was produced with:

```bash
python -m playwright install ffmpeg
pip install edge-tts imageio-ffmpeg
python docs/demo/tools/record_demo.py
```

The final demo must follow the beats in this folder's script/storyboard so the narration
matches the on-screen state.
