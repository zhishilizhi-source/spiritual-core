"""Image and short-video handling. Originals are kept byte-for-byte; resizing only happens on the way out."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# Videos are for the few clips worth keeping, not a storage room; also keeps the weekly git backup small.
MAX_VIDEO_BYTES = 50 * 1024 * 1024
# Long side sent to a model; larger images cost tokens without adding much.
MODEL_MAX_SIDE = 1568
THUMB_MAX_SIDE = 480

# MPO is a JPEG with extra pictures appended (phone HDR shots, some cameras); its first picture
# is a plain JPEG that every browser shows, so it is kept and served as one.
_FORMATS = {"JPEG": ("jpg", "image/jpeg"), "MPO": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"),
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


def is_video(mime: str) -> bool:
    return (mime or "").startswith("video/")


def _sniff_video(data: bytes) -> tuple[str, str] | None:
    """MP4 and QuickTime carry an 'ftyp' box at byte 4; WebM starts with the EBML magic."""
    if data[4:8] == b"ftyp":
        return ("mov", "video/quicktime") if data[8:10] == b"qt" else ("mp4", "video/mp4")
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm", "video/webm"
    return None


def probe(data: bytes) -> Probe:
    if not data:
        raise MediaError("文件是空的")
    video = _sniff_video(data)
    if video:
        if len(data) > MAX_VIDEO_BYTES:
            raise MediaError(f"视频太大了，上限 {MAX_VIDEO_BYTES // 1024 // 1024}MB，只放值得记住的那几秒")
        return Probe(video[0], video[1], 0, 0, None)
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


def video_tile(max_side: int = THUMB_MAX_SIDE) -> bytes:
    """Stand-in thumbnail for a video (no ffmpeg here to grab a frame): a dark tile with a play mark."""
    from PIL import ImageDraw
    w, h = max_side, max_side * 3 // 4
    im = Image.new("RGB", (w, h), (46, 42, 52))
    d = ImageDraw.Draw(im)
    r = h // 5
    cx, cy = w // 2, h // 2
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(236, 228, 240), width=max(2, r // 10))
    t = r // 2
    d.polygon([(cx - t * 2 // 3, cy - t), (cx - t * 2 // 3, cy + t), (cx + t, cy)], fill=(236, 228, 240))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return out.getvalue()
