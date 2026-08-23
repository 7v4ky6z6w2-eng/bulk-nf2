"""Validate ARTICLE.PHOTO blobs.

Per the user's decision: only ever use a photo that's already sitting in
the ERP database (a real, decodable embedded image). Never guess via web
search -- if PHOTO is empty or isn't a real image, the caller leaves the
WooCommerce product without an image, for manual upload later.
"""

import io
from dataclasses import dataclass
from typing import Optional

from PIL import Image, UnidentifiedImageError

_MIME_BY_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "WEBP": "image/webp",
    "TIFF": "image/tiff",
}
_EXT_BY_FORMAT = {
    "JPEG": "jpg", "PNG": "png", "GIF": "gif",
    "BMP": "bmp", "WEBP": "webp", "TIFF": "tiff",
}


@dataclass
class ExtractedImage:
    data: bytes
    mime_type: str
    filename: str


def extract_image(photo_blob, ref_art) -> Optional[ExtractedImage]:
    """Returns an ExtractedImage if 'photo_blob' is real, decodable image
    bytes; None if it's empty or not a valid image (caller should then
    leave the product's image field empty)."""
    if not photo_blob:
        return None

    try:
        img = Image.open(io.BytesIO(photo_blob))
        img.verify()  # cheap structural check, doesn't fully decode pixels
        fmt = img.format
    except (UnidentifiedImageError, OSError, ValueError):
        return None

    if fmt not in _MIME_BY_FORMAT:
        return None

    ext = _EXT_BY_FORMAT[fmt]
    safe_ref = "".join(c for c in str(ref_art) if c.isalnum() or c in "-_") or "article"
    return ExtractedImage(
        data=photo_blob,
        mime_type=_MIME_BY_FORMAT[fmt],
        filename=f"{safe_ref}.{ext}",
    )
