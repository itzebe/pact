"""Redact payment-method and contact details from the PayPal Sandbox receipt.

The receipt carries a cardholder name, a masked card number, and a backup card
number. None of that is needed as evidence, so it is blurred. The evidence
values are deliberately left untouched: amount, date, transaction id, seller
name, and order summary.

Usage:
  python redact_receipt.py <in.png> <out.png>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# (x0, y0, x1, y1) in full-screenshot coordinates.
REGIONS = [
    (48, 528, 700, 682),      # cardholder name and masked primary card
    (48, 783, 700, 842),      # "Your backup is ..." card line
    (48, 1143, 700, 1202),    # seller phone
    (48, 1243, 700, 1298),    # seller contact email
]
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def main() -> None:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    im = Image.open(src).convert("RGB")
    for box in REGIONS:
        im.paste(im.crop(box).filter(ImageFilter.GaussianBlur(11)), box[:2])
    d = ImageDraw.Draw(im)
    d.text((56, 692), "payment method and contact details redacted",
           font=ImageFont.truetype(FONT, 20), fill=(150, 150, 150))
    im.save(dst)
    print(f"wrote {dst} ({im.size[0]}x{im.size[1]})")


if __name__ == "__main__":
    main()
