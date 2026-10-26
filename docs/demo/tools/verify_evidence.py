"""Verify the enhanced PACT demo: three screenshots embedded, labelled, unaltered.

Checks against the rendered video:
  1. each supplied screenshot is present in its card, pixel-matched
  2. every value visible in the originals survives into the render
  3. the receipt's payment-method details stay redacted
  4. Sandbox evidence is labelled as Sandbox evidence
  5. no black frames and no silent stretches were introduced
  6. the original video is byte-unchanged

Usage:
  python verify_evidence.py <evidence_dir>
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "docs" / "demo"
VID = DEMO / "pact-demo-enhanced.mp4"
ORIG = DEMO / "pact-demo.mp4"

FF = subprocess.run([sys.executable, "-c",
                     "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
                    capture_output=True, text=True).stdout.strip()

SB, PANE_X, PANE_Y, PANE_H, PANE_W = 90, 64, 150, 660, 1472
SPLICE_A, SPLICE_B, FPS = 81.0, 155.7, 30

# card -> (screenshot, whole-column width, sample time inside the card)
CARDS = {
    "card_p": ("s3_redacted.png", 440, 86.0),
    "card_n": ("s2.png", 600, 160.0),
    "card_o": ("s1.png", 600, 170.0),
}

# text that must be readable in the render (values taken from the originals)
VALUES = {
    "card_p": (86.0, 440, ["$125.00", "October 25, 2026", "1T8850007E538313C", "Test Store"]),
    "card_n": (160.0, 600, ["$140.00", "$120.00", "ASK", "COUNTER", "USD"]),
    "card_o": (170.0, 600, ["$125.00", "$200.00", "Passed", "1.00", "escrow"]),
    "card_s": (96.0, 1472, ["9BU01766CS027323X", "7GB0704898318210B",
                            "05962303KN297374V", "COMPLETED", "settled", "released"]),
}

# must NOT be legible after redaction
REDACTED = ["9791", "9505", "202-784-6745", "qilf471120", "CREDIT UNION", "VISA"]


def frame(t: float) -> Image.Image:
    out = Path("/tmp/_pact_v2.png")
    subprocess.run([FF, "-y", "-ss", str(t), "-i", str(VID), "-frames:v", "1", str(out)],
                   capture_output=True)
    return Image.open(out).convert("RGB")


def ocr(im: Image.Image, psm: str = "11") -> str:
    p = Path("/tmp/_pact_v2_ocr.png")
    im.save(p)
    return subprocess.run(["tesseract", str(p), "-", "--psm", psm],
                          capture_output=True, text=True).stdout


def enlarged(t: float, col_w: int) -> Image.Image:
    """The enlarged column of a card, as a viewer reads it, upscaled 2x.

    OCR mode matters here: the column is a block of text, so psm 6 (assume a
    uniform block) reads it reliably, while psm 11 (sparse text) misses small
    runs. Whole-frame OCR is weaker still, because the card headline and caption
    dominate the page.
    """
    im = frame(t)
    x0 = PANE_X + col_w + 32
    col = im.crop((x0, PANE_Y, x0 + (PANE_W - col_w - 32), PANE_Y + PANE_H))
    return col.resize((col.width * 2, col.height * 2), Image.LANCZOS)


def zoom_at(t: float) -> float:
    start = SPLICE_A if t < SPLICE_B else SPLICE_B
    return min(1 + 0.030 * int((t - start) * FPS) / 900, 1.030)


def embedded(evidence: Path, shot: str, col_w: int, t: float) -> float:
    body = Image.open(evidence / shot).convert("RGB").crop((0, SB, 720, 1612))
    sc = min((col_w - 8) / body.width, PANE_H / body.height)
    ref = body.resize((round(body.width * sc), round(body.height * sc)),
                      Image.LANCZOS).convert("L")
    f = frame(t).convert("L")
    bw, bh = ref.size
    ox = PANE_X + (col_w - bw) // 2
    z = zoom_at(t)
    cx = round(800 + (ox - 800) * z)
    cy = round(450 + (PANE_Y - 450) * z)
    best = 1e9
    for dx in range(-12, 13, 2):
        for dy in range(-12, 13, 2):
            box = (cx + dx, cy + dy, cx + dx + bw, cy + dy + bh)
            if box[0] < 0 or box[1] < 0 or box[2] > f.width or box[3] > f.height:
                continue
            best = min(best, ImageStat.Stat(ImageChops.difference(f.crop(box), ref)).mean[0])
    return best


def main() -> int:
    evidence = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/ev3")
    bad = 0

    print("1. each supplied screenshot is embedded (pixel match)")
    for card, (shot, col_w, t) in CARDS.items():
        mad = embedded(evidence, shot, col_w, t)
        ok = mad < 20
        bad += not ok
        print(f"   {card} <- {shot}: MAD={mad:.1f} -> {'MATCH' if ok else 'NO MATCH'}")

    print("2. evidence values readable in the render")
    for card, (t, col_w, vals) in VALUES.items():
        # A value counts as readable if it is legible in either column: some
        # short words sit in the whole-screenshot column only.
        if card == "card_s":
            txt = ocr(frame(t)).lower()
        else:
            txt = (ocr(frame(t)) + "\n" + ocr(enlarged(t, col_w), "6")).lower()
        miss = [v for v in vals if v.lower() not in txt]
        bad += bool(miss)
        print(f"   {card} (t={t}s): {'ALL PRESENT' if not miss else 'MISSING ' + str(miss)}")

    print("3. redaction holds (payment-method details not legible)")
    p_txt = (ocr(frame(86.0)) + ocr(enlarged(86.0, 440), "6")).lower()
    leaked = [r for r in REDACTED if r.lower() in p_txt]
    bad += bool(leaked)
    print(f"   leaked: {leaked if leaked else 'none'}")

    print("4. Sandbox labelled as Sandbox")
    checks = {"sandbox label": "sandbox" in p_txt,
              "sandbox env host": "sandbox.paypal.com" in p_txt,
              "not-a-receipt note": "not a paypal receipt" in ocr(frame(96.0)).lower()}
    for k, v in checks.items():
        bad += not v
        print(f"   {k}: {'yes' if v else 'NO'}")

    print("5. black frames / silence")
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
    for i in range(8):
        t = round(i * total / 8, 1)
        q = subprocess.run([FF, "-hide_banner", "-ss", str(t), "-t", "3", "-i", str(VID),
                            "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
        mv = [l for l in q.splitlines() if "mean_volume" in l]
        if mv and float(mv[0].split("mean_volume:")[1].split("dB")[0]) < -60:
            silent += 1
    bad += bool(nblack) + bool(silent)
    print(f"   black intervals >=0.25s: {nblack}")
    print(f"   silent 3s windows: {silent}")

    print("6. original video unchanged")
    orig_sha = hashlib.sha256(ORIG.read_bytes()).hexdigest()
    expected = "a4ee93022845b5cc837e85e0881cdece44e826872f7eb16368191f4aac148e9e"
    ok = orig_sha == expected
    bad += not ok
    print(f"   pact-demo.mp4 sha256: {orig_sha[:16]}... {'OK' if ok else 'CHANGED'}")

    print("\nRESULT:", "PASS" if not bad else f"FAIL ({bad} problem(s))")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
