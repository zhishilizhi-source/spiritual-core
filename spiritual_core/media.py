"""Image handling. Originals are kept byte-for-byte; resizing only happens on the way out."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# Long side sent to a model; larger images cost tokens without adding much.
MODEL_MAX_SIDE = 1568
THUMB_MAX_SIDE = 480

_FORMATS = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"),
            "WEBP": ("webp", "image/webp"), "GIF": ("gif", "image/gif")}

_EXIF_IFD = 0x8769
_DATETIME_ORIGINAL = 0x9003
_DATETIME = 0x0132


class MediaError(ValueError):
    pass


@dataclass
class Probe:
    ext: str
    mime: str
    width: int
    height: int
    taken_on: str | None  # YYYY-MM-DD from EXIF, when the camera recorded it


def probe(data: bytes) -> Probe:
    if not data:
        raise MediaError("图片是空的")
    if len(data) > MAX_UPLOAD_BYTES:
        raise MediaError(f"图片太大了，上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            if fmt not in _FORMATS:
                raise MediaError(f"不支持 {fmt} 格式，只收 JPG / PNG / WEBP / GIF")
            ext, mime = _FORMATS[fmt]
            upright = ImageOps.exif_transpose(im)
            width, height = upright.size
            taken_on = _exif_date(im)
    except UnidentifiedImageError:
        raise MediaError("这不是能认出来的图片") from None
    return Probe(ext, mime, width, height, taken_on)


def _exif_date(im: Image.Image) -> str | None:
    try:
        exif = im.getexif()
        raw = exif.get_ifd(_EXIF_IFD).get(_DATETIME_ORIGINAL) or exif.get(_DATETIME)
        if not raw:
            return None
        return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S").date().isoformat()
    except Exception:
        return None


def render(data: bytes, max_side: int) -> bytes:
    """Upright, shrunk JPEG for models and thumbnails."""
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        if getattr(im, "is_animated", False):
            im.seek(0)
        im.thumbnail((max_side, max_side))
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, "white")
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.getchannel("A"))
            im = bg
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
        return out.getvalue()
