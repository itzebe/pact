"""Shared visual language for the PACT evidence cards (1600x900)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1600, 900
BG = (8, 9, 12)
PANEL = (17, 20, 27)
INK = (238, 241, 246)
DIM = (150, 160, 176)
FAINT = (112, 124, 144)
ACCENT = (96, 165, 250)
SAND = (250, 204, 21)          # sandbox / caution accent
LINE = (38, 44, 56)

BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

PANE = (64, 150, 1472, 660)
PANE_X, PANE_Y, PANE_W, PANE_H = PANE


def f(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def canvas(eyebrow: str, headline: str, caption: str, note: str,
           eyebrow_col=FAINT, note_col=FAINT) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((64, 42), eyebrow.upper(), font=f(REG, 21), fill=eyebrow_col)
    d.text((64, 74), headline, font=f(BOLD, 38), fill=INK)
    d.rectangle([PANE_X - 1, PANE_Y - 1, PANE_X + PANE_W, PANE_Y + PANE_H],
                outline=LINE, width=2)
    d.text((64, 832), caption, font=f(REG, 20), fill=DIM)
    d.text((64, 860), note, font=f(REG, 20), fill=note_col)
    return im


def bordered(img: Image.Image, label: str | None = None, col=LINE) -> Image.Image:
    """Thin frame, plus an optional label on a chip so it never merges with content."""
    out = img.copy()
    d = ImageDraw.Draw(out)
    d.rectangle([0, 0, out.width - 1, out.height - 1], outline=col, width=2)
    if label:
        fnt = f(REG, 19)
        tw = d.textlength(label, font=fnt)
        d.rounded_rectangle([8, 6, 8 + tw + 20, 36], radius=8, fill=(9, 11, 15))
        d.text((18, 11), label, font=fnt, fill=ACCENT)
    return out


def marks(img: Image.Image, src_box, sc: float, ox: int, oy: int, regions,
          col=ACCENT, r=9) -> None:
    """Outline regions of a crop that was scaled by sc and pasted at (ox, oy)."""
    d = ImageDraw.Draw(img)
    for (x0, y0, x1, y1) in regions:
        d.rounded_rectangle(
            [ox + round((x0 - src_box[0]) * sc) - r, oy + round((y0 - src_box[1]) * sc) - r,
             ox + round((x1 - src_box[0]) * sc) + r, oy + round((y1 - src_box[1]) * sc) + r],
            radius=10, outline=col, width=4)


def phone_body(path: Path, status_bar: int = 90) -> Image.Image:
    """Screenshot with the device status bar removed (not part of the evidence)."""
    return Image.open(path).convert("RGB").crop((0, status_bar, 720, 1612))
