"""Compose the three evidence cards used in docs/demo/pact-demo-enhanced.mp4.

Inputs are the two supplied PACT application captures
(docs/demo/evidence/pact-capture-outcome.png and -negotiation.png). Nothing is
redrawn: cards crop and scale the original pixels, and label them as
application records rather than PayPal receipts.

Run:  python evidence_cards.py            # writes cards into ./evidence-cards
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = ROOT / "docs" / "demo" / "evidence"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("evidence-cards")

W, H = 1600, 900
BG, PANEL = (8, 9, 12), (17, 20, 27)
INK, DIM, FAINT, ACCENT = (238, 241, 246), (139, 151, 168), (111, 123, 143), (96, 165, 250)
BOLD, REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", \
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

PANE = (64, 150, 1472, 660)
PANE_X, PANE_Y, PANE_H = PANE[0], PANE[1], PANE[3]
LEFT_W, GAP, RIGHT_W = 600, 32, 840
RIGHT_X = PANE_X + LEFT_W + GAP
STATUS_BAR = 90          # device clock/carrier/battery; dropped, not part of the evidence

S1 = EVIDENCE / "pact-capture-outcome.png"
S2 = EVIDENCE / "pact-capture-negotiation.png"


def f(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def card(headline: str, eyebrow: str, caption: str, note: str) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((64, 42), eyebrow.upper(), font=f(REG, 21), fill=FAINT)
    d.text((64, 74), headline, font=f(BOLD, 38), fill=INK)
    d.rectangle([PANE_X - 1, PANE_Y - 1, PANE_X + PANE[2], PANE_Y + PANE_H],
                outline=(38, 44, 56), width=2)
    d.text((64, 832), caption, font=f(REG, 20), fill=DIM)
    d.text((64, 860), note, font=f(REG, 20), fill=FAINT)
    return im


def inset(path: Path, region, label: str) -> Image.Image:
    """Whole phone capture with the enlarged region outlined."""
    full = Image.open(path).convert("RGB").crop((0, STATUS_BAR, 720, 1612))
    sc = PANE_H / full.height
    body = full.resize((round(full.width * sc), PANE_H), Image.LANCZOS)
    canvas = Image.new("RGB", (LEFT_W, PANE_H), PANEL)
    ox = (LEFT_W - body.width) // 2
    canvas.paste(body, (ox, 0))
    d = ImageDraw.Draw(canvas)
    x0, y0, x1, y1 = region
    d.rectangle([ox + round(x0 * sc), round((y0 - STATUS_BAR) * sc),
                 ox + round(x1 * sc), round((y1 - STATUS_BAR) * sc)], outline=ACCENT, width=3)
    d.rectangle([ox, 0, ox + body.width - 1, PANE_H - 1], outline=(44, 51, 64), width=2)
    d.text((10, 10), label, font=f(REG, 22), fill=ACCENT)
    return canvas


def detail(path: Path, box, highlights, label: str) -> Image.Image:
    im = Image.open(path).convert("RGB").crop(box)
    sc = RIGHT_W / im.width
    assert im.height * sc <= PANE_H, f"crop too tall: {im.height * sc:.0f}px"
    im = im.resize((RIGHT_W, round(im.height * sc)), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    for hx0, hy0, hx1, hy1 in highlights:
        d.rounded_rectangle(
            [round((hx0 - box[0]) * sc) - 8, round((hy0 - box[1]) * sc) - 8,
             round((hx1 - box[0]) * sc) + 8, round((hy1 - box[1]) * sc) + 8],
            radius=10, outline=ACCENT, width=4)
    d.text((10, 10), label, font=f(REG, 22), fill=ACCENT)
    return im


def build(out: Path, headline, eyebrow, caption, note, src, region, highlights) -> None:
    im = card(headline, eyebrow, caption, note)
    im.paste(inset(src, region, "Whole capture"), (PANE_X, PANE_Y))
    im.paste(detail(src, region, highlights, "Enlarged"), (RIGHT_X, PANE_Y))
    im.save(out)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    build(OUT / "card_a.png",
          "Settled job: outcome and escrow release",
          "Evidence 1 of 2  ·  PACT application capture",
          "Phone capture of pact-flame.vercel.app. PACT's own record of the settled job.",
          "This is the application's record, not a PayPal receipt.",
          S1, (60, 855, 660, 1300),
          [(105, 918, 252, 956), (160, 1178, 224, 1210), (398, 1153, 558, 1200)])

    build(OUT / "card_b.png",
          "Negotiation record",
          "Evidence 2 of 2  ·  PACT application capture",
          "Phone capture of pact-flame.vercel.app. Consistent with the settled job above: "
          "same $200 ceiling and seller Atlas.",
          "Shows the negotiation record only; it is not payment evidence.",
          S2, (60, 380, 660, 850),
          [(485, 483, 598, 519), (485, 771, 598, 807)])

    c = card("PACT's settlement record",
             "Application record  ·  not a PayPal receipt",
             "The escrow objects PACT holds for the settled job, served live from production.",
             "No PayPal screenshot was supplied for this job.")
    rec = Image.new("RGB", (PANE[2], 430), PANEL)
    d = ImageDraw.Draw(rec)
    rows = [("Status", "settled"), ("Escrow state", "released"),
            ("Provider / mode", "paypal / sandbox"), ("Amount", "$125.00"),
            ("Order", "9BU01766CS027323X"), ("Authorization", "7GB0704898318210B"),
            ("Capture", "05962303KN297374V"), ("Capture status", "COMPLETED")]
    for i, (k, v) in enumerate(rows):
        y = 26 + i * 49
        d.text((30, y), k, font=f(REG, 24), fill=FAINT)
        d.text((340, y), v, font=f(BOLD, 25), fill=INK)
    c.paste(rec, (PANE_X, PANE_Y))
    c.save(OUT / "card_c.png")
    print(f"wrote card_a.png, card_b.png, card_c.png to {OUT}")


if __name__ == "__main__":
    main()
