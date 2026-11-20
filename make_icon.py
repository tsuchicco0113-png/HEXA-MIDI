#!/usr/bin/env python3
"""HEXA MIDI のランチャー用アイコン: 光るピンクの六角形に鍵盤と音符
usage: python3 make_icon.py OUT.png [SIZE | REFERENCE.png]"""
import math
import sys

from PIL import Image, ImageDraw, ImageFilter


def hexpts(cx, cy, r):
    return [(cx + r * math.cos(math.radians(60 * i + 30)), cy + r * math.sin(math.radians(60 * i + 30)))
            for i in range(6)]


def make(size):
    s = size * 4
    u = s / 100
    pink = (255, 80, 170, 255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    pts = hexpts(50 * u, 50 * u, 44 * u)
    ImageDraw.Draw(glow).line(pts + [pts[0]], fill=(255, 80, 170, 200), width=int(10 * u))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(4 * u)))
    d = ImageDraw.Draw(img)
    d.polygon(pts, fill=(14, 20, 28, 255))
    d.line(pts + [pts[0]], fill=pink, width=int(4 * u), joint="curve")
    # 鍵盤
    x0, y0, w, h = 24, 58, 52, 20
    d.rectangle((x0 * u, y0 * u, (x0 + w) * u, (y0 + h) * u), fill=(235, 238, 245, 255))
    for i in range(1, 5):
        x = x0 + w * i / 5
        d.line((x * u, y0 * u, x * u, (y0 + h) * u), fill=(14, 20, 28, 255), width=max(1, int(1.2 * u)))
    for i in (1, 2, 4):
        x = x0 + w * i / 5
        d.rectangle(((x - 3.2) * u, y0 * u, (x + 3.2) * u, (y0 + 12) * u), fill=(14, 20, 28, 255))
    # 音符
    gold = (255, 214, 90, 255)
    d.ellipse((38 * u, 40 * u, 50 * u, 49 * u), fill=gold)
    d.rectangle((47 * u, 20 * u, 50 * u, 45 * u), fill=gold)
    d.polygon([(50 * u, 20 * u), (62 * u, 27 * u), (62 * u, 33 * u), (50 * u, 27 * u)], fill=gold)
    return img.resize((size, size), Image.LANCZOS)


def main():
    out, size = sys.argv[1], 64
    if len(sys.argv) > 2:
        arg = sys.argv[2]
        if arg.lower().endswith(".png"):
            try:
                size = Image.open(arg).size[0]
            except OSError:
                pass
        else:
            size = int(arg)
    make(size).save(out)
    print(f"icon {size}x{size} -> {out}")


if __name__ == "__main__":
    main()
