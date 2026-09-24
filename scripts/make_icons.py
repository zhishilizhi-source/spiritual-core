"""Draw the Spiritual Core icon: two crescents facing each other, a circle not quite closed.

The larger pale moon and the smaller terracotta one are the two of 双生.
Writes PNGs into spiritual_core/static/. Run from the repo root: python scripts/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

S = 2048                                   # draw big, shrink for smooth edges
BG = (31, 26, 23)
PALE = (239, 228, 214)
WARM = (208, 138, 114)
OUT = Path(__file__).resolve().parents[1] / "spiritual_core" / "static"


def disc(r: int, cx: int, cy: int) -> Image.Image:
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    return m


def crescent(cx: int, cy: int, r: int, cut_dx: int, cut_r: int) -> Image.Image:
    return ImageChops.subtract(disc(r, cx, cy), disc(cut_r, cx + cut_dx, cy))


def square(scale: float = 1.0) -> Image.Image:
    """Full-bleed art. scale < 1 leaves the safe margin Android's maskable icons need."""
    img = Image.new("RGB", (S, S), BG)
    c, r, gap = S // 2, int(S * 0.30 * scale), int(S * 0.045 * scale)
    img.paste(PALE, (0, 0), crescent(c - gap, c, r, int(r * 0.52), int(r * 0.93)))
    img.paste(WARM, (0, 0), crescent(c + gap, c, int(r * 0.78), -int(r * 0.40), int(r * 0.72)))
    return img


def rounded(img: Image.Image) -> Image.Image:
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def main() -> None:
    full, safe = square(), square(0.82)
    rounded(full).resize((64, 64), Image.LANCZOS).save(OUT / "favicon.png")
    full.resize((180, 180), Image.LANCZOS).save(OUT / "apple-touch-icon.png")   # iOS rounds it
    for size in (192, 512):
        safe.resize((size, size), Image.LANCZOS).save(OUT / f"icon-{size}.png")
    print("icons ->", OUT)


if __name__ == "__main__":
    main()
