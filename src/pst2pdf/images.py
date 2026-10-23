"""Image normalization shared by inline images and image attachments.

Phone photos arrive at 12 megapixels and, stored as raw pixels, make a
single page weigh 40 MB. Nothing about redaction needs more than a Letter
page at 200 dpi, so large images are downscaled and photos are stored as
JPEG. Small images (signature logos, icons) pass through untouched.
"""

from __future__ import annotations

import io
import logging

from PIL import Image

log = logging.getLogger(__name__)

MAX_SIDE = 2200  # pixels on the longer side after normalization
JPEG_PIXELS = 1_500_000  # above this many pixels, store as JPEG
JPEG_QUALITY = 85
MIN_VISIBLE = 96  # an image smaller than this in either direction is an icon, not content

#: Largest image Pillow will decode anywhere in the pipeline. A 50-megapixel
#: RGB image is 150 MB of raw pixels; a crafted header claiming more is a
#: decompression bomb. Pillow warns above this and raises
#: ``DecompressionBombError`` above twice it; ``convert`` also refuses
#: anything over the limit itself, so the cap is exact for attachments.
MAX_IMAGE_PIXELS = 50_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

try:  # HEIC/HEIF (iPhone photos) need a plugin; keep working without it
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:  # noqa: BLE001
    HEIF_SUPPORTED = False


def probe(data: bytes) -> tuple[int, int] | None:
    """(width, height) of an encoded image, or None when Pillow cannot read it."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return None


def is_visible(size: tuple[int, int] | None) -> bool:
    """Big enough that it could carry a name or a face and need area redaction."""
    return size is not None and size[0] >= MIN_VISIBLE and size[1] >= MIN_VISIBLE


def normalize(data: bytes, content_type: str) -> tuple[bytes, str]:
    """Return (bytes, content_type) safe to embed: downscaled and JPEG-encoded
    when large, otherwise the original bytes."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if max(w, h) <= MAX_SIDE and w * h <= JPEG_PIXELS:
                return data, content_type
            if getattr(im, "n_frames", 1) > 1:
                return data, content_type  # animated: leave as is
            im.load()
            if max(w, h) > MAX_SIDE:
                scale = MAX_SIDE / max(w, h)
                im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
            return encode_jpeg(im), "image/jpeg"
    except Exception as exc:  # noqa: BLE001
        log.debug("image left as is (%s)", exc)
        return data, content_type


def encode_jpeg(im) -> bytes:
    """Flatten transparency onto white and encode as JPEG."""
    from PIL import Image

    if im.mode in ("RGBA", "LA", "P", "PA"):
        rgba = im.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.getchannel("A"))
        im = base
    elif im.mode != "RGB" and im.mode != "L":
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()
