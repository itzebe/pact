# How the demo video was produced

The final video is `docs/demo/pact-demo.mp4`. It is assembled from genuine screen
captures of the deployed application and the real PayPal Sandbox checkout page.
No footage is mocked, and no synthetic "live" response is presented as real.

## Toolchain

| Need | Tool used |
|---|---|
| Drive the real site and record it | Playwright (Python) driving the system Chromium (`/usr/bin/chromium`), `record_video` at 1600×900 |
| Narration | `edge-tts` (Microsoft Edge neural TTS, voice `en-US-GuyNeural`) |
| Title card and lower-third labels | Pillow |
| Trim, normalise, label, mux, concat | ffmpeg via `imageio-ffmpeg` |

ffmpeg from `imageio-ffmpeg` has no `drawtext`, so all text is rendered with
Pillow and composited with the `overlay` filter. Playwright's own video encoder
needs its bundled ffmpeg (`python -m playwright install ffmpeg`); the resulting
`.webm` files are VP9 and are re-encoded to H.264 here.

## Reproduce

```bash
pip install playwright edge-tts pillow imageio-ffmpeg
python -m playwright install ffmpeg          # Playwright's video encoder
python docs/demo/tools/record_demo.py        # narration + per-scene recordings
python docs/demo/tools/assemble_demo.py      # title card, labels, final mp4
```

`record_demo.py` records one clip per narration beat so every scene shows the
panel its narration describes, and writes `scene_clips.json`. `assemble_demo.py`
normalises each clip to 1600×900, trims it to the beat, adds the environment
label, muxes the narration, and concatenates the scenes.

## Honesty notes

- The app footage is a real capture of <https://pact-flame.vercel.app/> against
  PayPal Sandbox. The settlement shown (`job_ca853a58b442`) is a real production
  record with a real sandbox capture.
- The PayPal checkout is the real `www.sandbox.paypal.com` page for a real sandbox
  order. The recording stops at the login prompt: **completing the buyer approval
  requires a logged-in sandbox buyer account, which the automated environment does
  not have.** The approval itself is therefore not shown, and the narration says so
  ("PayPal requires the buyer to approve first, so Pact surfaces that step
  honestly") rather than implying an approval happened on camera.
- No real money is involved anywhere. Sandbox only.
