"""Generate the title card and lower-third authenticity labels for the demo."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BUILD = Path(__file__).resolve().parent / "build"
BUILD.mkdir(parents=True, exist_ok=True)
W, H = 1600, 900

INK950 = (8, 9, 12)
INK850 = (18, 21, 28)
PAPER = (238, 241, 246)
PAPER_DIM = (167, 176, 192)
PAPER_FAINT = (107, 116, 136)
PAYPAL = (43, 159, 224)
RELEASED = (62, 207, 142)

BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def title_card() -> None:
    img = Image.new("RGB", (W, H), INK950)
    d = ImageDraw.Draw(img)

    # subtle top band
    d.rectangle([0, 0, W, 6], fill=PAYPAL)

    x = 140
    d.text((x, 210), "PACT", font=font(BOLD, 132), fill=PAPER)
    d.text((x, 372), "Settlement infrastructure for the agent economy",
           font=font(REG, 46), fill=PAPER_DIM)

    d.line([x, 470, x + 640, 470], fill=INK850, width=2)
    d.text((x, 500), "Conditional PayPal escrow   ·   independent verification   ·   reputation-aware market",
           font=font(REG, 30), fill=PAPER_FAINT)

    d.text((x, 640), "Live demo", font=font(BOLD, 26), fill=RELEASED)
    d.text((x + 170, 640), "pact-flame.vercel.app", font=font(REG, 26), fill=PAPER_DIM)
    d.text((x, 684), "Source", font=font(BOLD, 26), fill=RELEASED)
    d.text((x + 170, 684), "github.com/itzebe/pact", font=font(REG, 26), fill=PAPER_DIM)

    d.text((x, 790), "Footage captured live from the deployed app and the real PayPal Sandbox (test environment).",
           font=font(REG, 24), fill=PAPER_FAINT)
    img.save(BUILD / "title.png")


def label(text: str, path: Path, accent: tuple[int, int, int]) -> None:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = font(BOLD, 24)
    pad = 18
    box = d.textbbox((0, 0), text, font=f)
    tw, th = box[2] - box[0], box[3] - box[1]
    x0, y0 = 40, H - 92
    x1, y1 = x0 + tw + pad * 2 + 16, y0 + th + pad * 2
    d.rounded_rectangle([x0, y0, x1, y1], radius=10, fill=(8, 9, 12, 210))
    d.ellipse([x0 + pad, y0 + pad + th // 2 - 6, x0 + pad + 12, y0 + pad + th // 2 + 6], fill=accent)
    d.text((x0 + pad + 24, y0 + pad - box[1]), text, font=f, fill=PAPER)
    img.save(path)


if __name__ == "__main__":
    title_card()
    label("LIVE APP  ·  pact-flame.vercel.app", BUILD / "label_app.png", RELEASED)
    label("PAYPAL SANDBOX  ·  real checkout page (test environment)", BUILD / "label_paypal.png", PAYPAL)
    print("assets written")
