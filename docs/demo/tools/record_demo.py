"""Record the demo footage: synthesise narration, then capture the live app and
the real PayPal Sandbox checkout with Playwright.

One clip is recorded per narration beat so every scene shows the panel its
narration describes. Outputs land in ./build next to this file.

    python -m playwright install ffmpeg   # Playwright's video encoder, once
    python record_demo.py
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import edge_tts
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
SCENES = BUILD / "scenes"
SCENES.mkdir(parents=True, exist_ok=True)

APP = "https://pact-flame.vercel.app/"
# The real sandbox order left awaiting buyer approval, shown in the video.
PAYPAL = "https://www.sandbox.paypal.com/checkoutnow?token=9GW7441588229311T"
SETTLED_JOB = re.compile("Landing page copy for a fintech launch")
W, H = 1600, 900
VOICE = "en-US-GuyNeural"


def ff() -> str:
    return subprocess.run(
        [sys.executable, "-c", "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
        capture_output=True, text=True, check=True).stdout.strip()


def dur(path: Path) -> float:
    o = subprocess.run([ff(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", o)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def synth(segments: dict[str, str]) -> dict[str, float]:
    out = {}
    for key, text in segments.items():
        mp3 = BUILD / f"{key}.mp3"
        if not mp3.exists():
            asyncio.run(edge_tts.Communicate(text, VOICE).save(str(mp3)))
        out[key] = dur(mp3)
    return out


def record(name: str, seconds: float, action, url: str = APP, wait: str = "networkidle") -> Path:
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        ctx = b.new_context(viewport={"width": W, "height": H},
                            record_video_dir=str(SCENES / name),
                            record_video_size={"width": W, "height": H})
        pg = ctx.new_page()
        pg.goto(url, wait_until=wait, timeout=90000)
        pg.wait_for_timeout(1800)
        action(pg)
        pg.wait_for_timeout(int(seconds * 1000) + 1200)
        video = pg.video.path()  # must be read before the context closes
        ctx.close()
        b.close()
    return Path(video)


def open_job(pg) -> None:
    pg.get_by_role("button", name=SETTLED_JOB).first.click()
    pg.wait_for_timeout(1500)


def tab(label: str):
    def _inner(pg) -> None:
        open_job(pg)
        try:
            pg.get_by_role("button", name=label).click()
            pg.wait_for_timeout(1400)
        except Exception:
            pass
    return _inner


def main() -> None:
    segments = json.loads((HERE / "narration.json").read_text())
    d = synth(segments)
    print("narration:", {k: round(v, 1) for k, v in d.items()})
    print("total:", round(sum(d.values()), 1), "s")

    clips = {
        "s1": record("s1_problem", d["s1_problem"], lambda pg: None),
        "s2": record("s2_agreement", d["s2_agreement"], tab("Negotiation")),
        "s3a": record("s3a_escrow", max(6.0, d["s3_payment"] - 10.0), open_job),
        "s3b": record("s3b_paypal", 10.0, lambda pg: None, url=PAYPAL, wait="domcontentloaded"),
        "s4": record("s4_safety", d["s4_safety"], tab("Verification")),
        "s5": record("s5_ledger", d["s5_ledger"], tab("Ledger")),
        "s6": record("s6_close", d["s6_close"], lambda pg: None),
    }
    (BUILD / "scene_clips.json").write_text(json.dumps({k: str(v) for k, v in clips.items()}, indent=1))
    for k, v in clips.items():
        print(f"{k}: {v.name} ({dur(v):.1f}s)")


if __name__ == "__main__":
    main()
