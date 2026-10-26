"""Compose the evidence cards for the PACT demo (1600x900).

Cards crop and scale the supplied screenshots; nothing is redrawn. Crop boxes
are in *body* coordinates, i.e. after the 90px device status bar is dropped.

Usage:
  python evidence_cards.py <evidence_dir> <out_dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
from evidence_design import (ACCENT, FAINT, INK, MONO, PANE_H, PANE_W, PANE_X,
                             PANE_Y, PANEL, REG, SAND, bordered, canvas, f,
                             marks, phone_body)

GAP = 32
SB = 90


def whole(path: Path, width: int, regions=(), col=ACCENT) -> Image.Image:
    body = phone_body(path, SB)
    sc = min((width - 8) / body.width, PANE_H / body.height)
    im = body.resize((round(body.width * sc), round(body.height * sc)), Image.LANCZOS)
    c = Image.new("RGB", (width, PANE_H), PANEL)
    ox, oy = (width - im.width) // 2, (PANE_H - im.height) // 2
    c.paste(im, (ox, oy))
    if regions:
        marks(c, (0, 0, body.width, body.height), sc, ox, oy, regions, col=col, r=6)
    return bordered(c, "Whole screenshot")


def detail_n(path: Path, width: int, boxes, regions=(), col=ACCENT,
             gap: int = 14) -> Image.Image:
    """N enlarged strips stacked, scaled so the stack always fits the pane."""
    body = phone_body(path, SB)
    src_w = max(b[2] - b[0] for b in boxes)
    sc = width / src_w
    heights = [round((b[3] - b[1]) * sc) for b in boxes]
    total = sum(heights) + gap * (len(boxes) - 1)
    if total > PANE_H:
        sc *= (PANE_H - gap * (len(boxes) - 1)) / sum(heights)
        heights = [round((b[3] - b[1]) * sc) for b in boxes]
        total = sum(heights) + gap * (len(boxes) - 1)
    c = Image.new("RGB", (width, PANE_H), PANEL)
    oy = (PANE_H - total) // 2
    for box, h in zip(boxes, heights):
        strip = body.crop(box).resize((round((box[2] - box[0]) * sc), h), Image.LANCZOS)
        c.paste(strip, (0, oy))
        if regions:
            marks(c, box, sc, 0, oy, regions, col=col, r=8)
        oy += h + gap
    return bordered(c, "Enlarged")


def build(out: Path, name, eyebrow, headline, caption, note, whole_img,
          detail_img, eyebrow_col=FAINT, note_col=FAINT) -> None:
    im = canvas(eyebrow, headline, caption, note, eyebrow_col, note_col)
    im.paste(whole_img, (PANE_X, PANE_Y))
    im.paste(detail_img, (PANE_X + whole_img.width + GAP, PANE_Y))
    im.save(out / f"{name}.png")
    print(f"  {name}.png")


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/ev3")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/ev3/build")
    out.mkdir(parents=True, exist_ok=True)

    P = src / "s3_redacted.png"      # genuine PayPal Sandbox receipt
    N = src / "s2.png"               # PACT negotiation capture
    O = src / "s1.png"               # PACT settled-outcome capture

    build(out, "card_p",
          "Evidence 1 of 3  ·  PayPal Sandbox",
          "PayPal Sandbox — $125.00 Payment Record",
          "PayPal Sandbox receipt (test environment, sandbox.paypal.com) supplied as evidence.",
          "Sandbox test payment — no real money moved. Payment method and contact details redacted.",
          whole(P, 440, regions=[(126, 110, 694, 216), (50, 790, 412, 900),
                                 (198, 1240, 694, 1360)]),
          detail_n(P, 1000,
                   [(40, 110, 700, 230), (40, 790, 700, 910), (40, 1240, 700, 1470)],
                   regions=[(500, 126, 694, 174), (126, 178, 578, 218),
                            (50, 856, 412, 900), (198, 1300, 694, 1356)]),
          eyebrow_col=SAND, note_col=SAND)

    build(out, "card_n",
          "Evidence 2 of 3  ·  PACT application capture",
          "Negotiation record",
          "Phone capture of pact-flame.vercel.app: round-one ask and the buyer's counter.",
          "PACT's own application record — not a PayPal receipt.",
          whole(N, 600),
          detail_n(N, 840, [(60, 380, 660, 850)],
                   regions=[(485, 483, 598, 519), (485, 771, 598, 807)]))

    build(out, "card_o",
          "Evidence 3 of 3  ·  PACT application capture",
          "Settled job: outcome and escrow release",
          "Phone capture of pact-flame.vercel.app: verification passed, $125.00 released to the seller.",
          "PACT's own application record — not a PayPal receipt.",
          whole(O, 600),
          detail_n(O, 840, [(60, 855, 660, 1300)],
                   regions=[(105, 918, 252, 956), (160, 1178, 224, 1210),
                            (398, 1153, 558, 1200)]))

    c = canvas("PACT application record  ·  not a PayPal receipt",
               "PACT's settlement record",
               "The escrow objects PACT holds for the settled job, served live from production.",
               "A different purpose from the receipt: this is PACT's own audit record, not PayPal's.")
    rec = Image.new("RGB", (PANE_W, 470), PANEL)
    d = ImageDraw.Draw(rec)
    rows = [("Status", "settled"), ("Escrow state", "released"),
            ("Provider / mode", "paypal / sandbox"), ("Amount", "$125.00"),
            ("Order", "9BU01766CS027323X"), ("Authorization", "7GB0704898318210B"),
            ("Capture", "05962303KN297374V"), ("Capture status", "COMPLETED")]
    for i, (k, v) in enumerate(rows):
        y = 26 + i * 54
        d.text((30, y), k, font=f(REG, 24), fill=FAINT)
        d.text((340, y), v, font=f(MONO, 25), fill=INK)
    c.paste(rec, (PANE_X, PANE_Y))
    c.save(out / "card_s.png")
    print("  card_s.png")


if __name__ == "__main__":
    main()
