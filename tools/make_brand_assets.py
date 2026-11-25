"""Builds the logo files used by the UI, the report and the .exe from assets/connectm_logo_source.png.

    python tools/make_brand_assets.py          (needs Pillow: pip install pillow)

The source is a small screenshot (303x66). It is cleaned and enlarged so the logo stays sharp on high-DPI screens:
the artwork is cut out of the screenshot's badge (its off-white background becomes transparent), enlarged 4x with a
high-quality filter, lightly sharpened, and placed on a clean white rounded badge that works on both the light and dark theme.

Outputs (all committed, so building the .exe does not need Pillow):
    canbench/web/connectm_logo.png   full "ConnectM" badge, ~4x resolution (header corner, Info page, report)
    canbench/web/connectm_mark.png   just the chevron mark, transparent (browser tab icon)
    assets/CANBench.ico              exe / window icon (mark on a white rounded square, 16..256 px)
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "assets", "connectm_logo_source.png")
K = 4          # enlargement factor


def rounded_mask(size, radius, scale=4):
    w, h = size
    m = Image.new("L", (w * scale, h * scale), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * scale - 1, h * scale - 1), radius=radius * scale, fill=255)
    return m.resize((w, h), Image.LANCZOS)


def cut_out(im, box):
    """The coloured artwork inside `box` with the near-white background made transparent."""
    art = im.crop(box).convert("RGBA")
    px = art.load()
    for y in range(art.height):
        for x in range(art.width):
            r, g, b, _a = px[x, y]
            d = min(r, g, b)                              # colour pixels are far from white, the background is close to it
            px[x, y] = (r, g, b, max(0, min(255, int((246 - d) * 255 / 120))))
    return art


def main():
    im = Image.open(SRC).convert("RGBA")
    px = im.load()
    xs, ys = [], []
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, _a = px[x, y]
            if r > 235 and g > 235 and b > 235:
                xs.append(x)
                ys.append(y)
    bx0, by0, bx1, by1 = min(xs), min(ys), max(xs) + 1, max(ys) + 1          # the screenshot's own white badge
    inner = (bx0 + 10, by0 + 6, bx1 - 8, by1 - 6)                           # artwork only: stay clear of its rounded corners
    art = cut_out(im, inner)
    art = art.crop(art.getbbox())
    art = art.resize((art.width * K, art.height * K), Image.LANCZOS).filter(ImageFilter.UnsharpMask(radius=2, percent=110, threshold=2))

    # 1. full logo: artwork on a clean white rounded badge
    padx, pady = 14 * K, 8 * K
    badge = Image.new("RGBA", (art.width + 2 * padx, art.height + 2 * pady), (255, 255, 255, 255))
    badge.alpha_composite(art, (padx, pady))
    badge.putalpha(rounded_mask(badge.size, 11 * K))
    badge.save(os.path.join(ROOT, "canbench", "web", "connectm_logo.png"), optimize=True)

    # 2. the chevron mark (left part of the artwork)
    mark = cut_out(im, (bx0 + 14, by0 + 5, bx0 + 50, by0 + 48))
    mark = mark.crop(mark.getbbox())
    mark = mark.resize((mark.width * K, mark.height * K), Image.LANCZOS)
    mark.save(os.path.join(ROOT, "canbench", "web", "connectm_mark.png"), optimize=True)

    # 3. icon: the mark on a white rounded square
    big = 256
    icon = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    sq = Image.new("RGBA", (big, big), (255, 255, 255, 255))
    sq.putalpha(rounded_mask((big, big), 52))
    icon.alpha_composite(sq)
    k = int(big * 0.60) / max(mark.size)
    m2 = mark.resize((max(1, int(mark.width * k)), max(1, int(mark.height * k))), Image.LANCZOS)
    icon.alpha_composite(m2, ((big - m2.width) // 2, (big - m2.height) // 2))
    os.makedirs(os.path.join(ROOT, "assets"), exist_ok=True)
    icon.save(os.path.join(ROOT, "assets", "CANBench.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    icon.save(os.path.join(ROOT, "assets", "CANBench_icon.png"))
    print("logo", badge.size, "mark", mark.size)


if __name__ == "__main__":
    sys.exit(main())
