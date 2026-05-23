#!/usr/bin/env python3
"""Generate the branded YantrAI agent icon (run once; outputs committed).

Draws a rounded-square tile in the brand gradient (#da7756 -> #e8a87c) with a
bold white "Y", and exports:
  assets/yantrai_256.png   — for the Tk window + tray image
  assets/yantrai.ico       — multi-size icon for the .exe + Windows taskbar
"""
import os
from PIL import Image, ImageDraw, ImageFont

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
os.makedirs(ASSETS, exist_ok=True)

SIZE = 256
RADIUS = 56
C1 = (218, 119, 86)    # #da7756
C2 = (232, 168, 124)   # #e8a87c


def _vertical_gradient(w, h, top, bottom):
    base = Image.new("RGB", (w, h), top)
    top_r, top_g, top_b = top
    bot_r, bot_g, bot_b = bottom
    px = base.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top_r + (bot_r - top_r) * t)
        g = int(top_g + (bot_g - top_g) * t)
        b = int(top_b + (bot_b - top_b) * t)
        for x in range(w):
            px[x, y] = (r, g, b)
    return base


def _rounded_mask(w, h, radius):
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    return mask


def _load_font(size):
    for name in ("seguibl.ttf", "segoeuib.ttf", "arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def build():
    grad = _vertical_gradient(SIZE, SIZE, C1, C2).convert("RGBA")
    mask = _rounded_mask(SIZE, SIZE, RADIUS)
    tile = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    tile.paste(grad, (0, 0), mask)

    # Bold "Y" glyph, centered, white with a soft shadow.
    draw = ImageDraw.Draw(tile)
    font = _load_font(168)
    text = "Y"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (SIZE - tw) / 2 - bbox[0]
    y = (SIZE - th) / 2 - bbox[1] - 6
    draw.text((x + 3, y + 4), text, font=font, fill=(120, 50, 30, 90))   # shadow
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))        # glyph

    png_path = os.path.join(ASSETS, "yantrai_256.png")
    tile.save(png_path, "PNG")

    ico_path = os.path.join(ASSETS, "yantrai.ico")
    tile.save(ico_path, "ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    print("Wrote", png_path)
    print("Wrote", ico_path)


if __name__ == "__main__":
    build()
