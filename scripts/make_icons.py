"""Draw the Spiritual Core icon: two crescents facing each other, a circle not quite closed.

The larger pale moon and the smaller terracotta one are the two of 双生.
Writes PNGs into spiritual_core/static/. Run from the repo root: python scripts/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

S = 2048                                   # draw big, shrink for smooth edges
# Dusk sinking into night: the moment both moons are in the sky.
SKY = [(0.0, (238, 184, 162)), (0.45, (160, 120, 150)), (1.0, (66, 56, 94))]
PALE = (255, 247, 236)
WARM = (236, 158, 132)
OUT = Path(__file__).resolve().parents[1] / "spiritual_core" / "static"


def disc(r: int, cx: int, cy: int) -> Image.Image:
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    return m


def crescent(cx: int, cy: int, r: int, cut_dx: int, cut_r: int) -> Image.Image:
    return ImageChops.subtract(disc(r, cx, cy), disc(cut_r, cx + cut_dx, cy))


def sky() -> Image.Image:
    img = Image.new("RGB", (S, S))
    d = ImageDraw.Draw(img)
    for y in range(S):
        t = y / (S - 1)
        for (t0, c0), (t1, c1) in zip(SKY, SKY[1:]):
            if t0 <= t <= t1:
                k = (t - t0) / (t1 - t0)
                d.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * k) for a, b in zip(c0, c1)))
                break
    return img


def square(scale: float = 1.0) -> Image.Image:
    """Full-bleed art. scale < 1 leaves the safe margin Android's maskable icons need."""
    img = sky()
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
