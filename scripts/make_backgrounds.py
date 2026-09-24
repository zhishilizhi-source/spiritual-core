"""Turn her paintings into washed-out page backgrounds for the light scheme.

Reads image records from the library, writes softened copies to <data>/backgrounds/
plus an index.json the page reads. Her art stays in the data folder, never in the repo.

usage: python scripts/make_backgrounds.py <data_dir> <record_id> [<record_id> ...]
"""

import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spiritual_core.store import Store  # noqa: E402

LONG_SIDE = 1400
WASH = 0.66          # how much white paper covers the paint
BLUR = 1.6           # soften detail but keep the brushwork
SATURATION = 0.85


def wash(data: bytes) -> Image.Image:
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    im.thumbnail((LONG_SIDE, LONG_SIDE))
    im = im.filter(ImageFilter.GaussianBlur(BLUR))
    im = ImageEnhance.Color(im).enhance(SATURATION)
    paper = Image.new("RGB", im.size, (252, 250, 247))
    return Image.blend(im, paper, WASH)


def main() -> None:
    data_dir = Path(sys.argv[1])
    out = data_dir / "backgrounds"
    out.mkdir(exist_ok=True)
    store = Store(data_dir / "core.db")
    names = []
    for rid in sys.argv[2:]:
        _, _, data = store.image(rid)
        name = f"{rid}.jpg"
        wash(data).save(out / name, quality=86, optimize=True, progressive=True)
        names.append(name)
    (out / "index.json").write_text(json.dumps(names), encoding="utf-8")
    print(f"{len(names)} backgrounds -> {out}")


if __name__ == "__main__":
    main()
