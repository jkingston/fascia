"""Regenerate font.bin from the pinned DejaVuSans.ttf; requires Pillow."""

import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

font = ImageFont.truetype(sys.argv[1], 32)
advances, pixels = bytearray(), bytearray()
for code in range(32, 127):
    image = Image.new("L", (40, 48))
    ImageDraw.Draw(image).text((0, 0), chr(code), font=font, fill=255)
    advances.append(round(font.getlength(chr(code))))
    pixels.extend(image.tobytes())
Path(__file__).with_name("font.bin").write_bytes(advances + pixels)
