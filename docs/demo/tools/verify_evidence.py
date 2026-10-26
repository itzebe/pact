"""Verify that the two supplied captures are genuinely embedded, unaltered.

Checks, against the rendered video:
  1. the 'whole capture' inset of each card still matches the original pixels
  2. every value visible in the originals survives into the render
  3. no black frames and no silent stretches were introduced

Run:  python verify_evidence.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "docs" / "demo"
VID = DEMO / "pact-demo-enhanced.mp4"
EVIDENCE = DEMO / "evidence"

FF = subprocess.run([sys.executable, "-c",
                     "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
                    capture_output=True, text=True).stdout.strip()

STATUS_BAR, PANE_X, PANE_Y, PANE_H, LEFT_W = 90, 64, 150, 660, 600
SPLICE, FPS = 133.0, 30


def frame(t: float) -> Image.Image:
    out = Path("/tmp/_pact_verify.png")
    subprocess.run([FF, "-y", "-ss", str(t), "-i", str(VID), "-frames:v", "1", str(out)],
                   capture_output=True)
    return Image.open(out).convert("RGB")


def ocr(im: Image.Image) -> str:
    p = Path("/tmp/_pact_ocr.png")
    im.save(p)
    return subprocess.run(["tesseract", str(p), "-", "--psm", "11"],
                          capture_output=True, text=True).stdout


def zoom_at(t: float) -> float:
    return min(1 + 0.035 * int((t - SPLICE) * FPS) / 900, 1.035)


def check_embedded(card: str, shot: str, t: float) -> float:
    full = Image.open(EVIDENCE / shot).convert("RGB").crop((0, STATUS_BAR, 720, 1612))
    sc = PANE_H / full.height
    ref = full.resize((round(720 * sc), PANE_H), Image.LANCZOS).convert("L")
    f = frame(t).convert("L")
    bw, bh = ref.size
    z = zoom_at(t)
    cx = round(800 + (PANE_X + (LEFT_W - bw) // 2 - 800) * z)
    cy = round(450 + (PANE_Y - 450) * z)
    best = 1e9
    for dx in range(-10, 11, 2):
        for dy in range(-10, 11, 2):
            box = (cx + dx, cy + dy, cx + dx + bw, cy + dy + bh)
            if box[0] < 0 or box[1] < 0 or box[2] > f.width or box[3] > f.height:
                continue
            best = min(best, ImageStat.Stat(ImageChops.difference(f.crop(box), ref)).mean[0])
    return best


VALUES = {
    "card_a": (137.0, ["$125.00", "$200.00", "Passed", "atlas", "1.00", "escrow",
                       "VERIFICATION", "RELEASED", "SELLER"]),
    "card_b": (151.0, ["$140.00", "$120.00", "USD", "ASK", "COUNTER", "seller_atlas"]),
    "card_c": (160.0, ["9BU01766CS027323X", "7GB0704898318210B", "05962303KN297374V",
                       "COMPLETED", "released", "sandbox", "settled"]),
}


def main() -> int:
    bad = 0
    print("1. embedded-pixel check")
    for card, shot, t in [("card_a", "pact-capture-outcome.png", 140.0),
                          ("card_b", "pact-capture-negotiation.png", 151.0)]:
        mad = check_embedded(card, shot, t)
        ok = mad < 18
        bad += not ok
        print(f"   {card} vs {shot}: MAD={mad:.1f} -> {'MATCH' if ok else 'NO MATCH'}")

    print("2. displayed values")
    for card, (t, vals) in VALUES.items():
        txt = ocr(frame(t)).lower()
        miss = [v for v in vals if v.lower() not in txt]
        bad += bool(miss)
        print(f"   {card} (t={t}s): {'ALL PRESENT' if not miss else 'MISSING ' + str(miss)}")

    print("3. black frames / silence")
    p = subprocess.run([FF, "-hide_banner", "-i", str(VID), "-vf",
                        "blackdetect=d=0.25:pix_th=0.06", "-f", "null", "-"],
                       capture_output=True, text=True)
    nblack = sum("black_start" in l for l in p.stderr.splitlines())
    err = subprocess.run([FF, "-hide_banner", "-i", str(VID)],
                         capture_output=True, text=True).stderr
    line = [l for l in err.splitlines() if "Duration" in l][0]
    h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
    total = int(h) * 3600 + int(m) * 60 + float(s)
    silent = 0
    for i in range(10):
        t = round(i * total / 10, 1)
        q = subprocess.run([FF, "-hide_banner", "-ss", str(t), "-t", "3", "-i", str(VID),
                            "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
        mv = [l for l in q.splitlines() if "mean_volume" in l]
        if mv and float(mv[0].split("mean_volume:")[1].split("dB")[0]) < -60:
            silent += 1
    bad += bool(nblack) + bool(silent)
    print(f"   black intervals >=0.25s: {nblack}")
    print(f"   silent 3s windows: {silent}")
    print("\nRESULT:", "PASS" if not bad else f"FAIL ({bad} problem(s))")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
