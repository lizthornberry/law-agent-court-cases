"""Image preparation: EXIF orientation, optional page rotation, downscale, JPEG bytes."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

from .config import Config


def prepare_image_bytes(
    path: Path,
    cfg: Config,
    rotate_degrees: int = 0,
) -> bytes:
    """Load an image, apply EXIF + optional page rotation, downscale, return JPEG bytes.

    ``rotate_degrees`` is the amount the page must be rotated counter-clockwise
    to read upright (0 / 90 / 180 / 270). It comes from Pass A's
    ``detected_rotation_degrees`` and is applied *after* EXIF transpose so a
    sideways camera photo is sent to the model right-way-up rather than relying
    on the model to "mentally rotate".
    """
    max_dim = int(cfg.get("image", "max_dimension", default=2200))
    quality = int(cfg.get("image", "jpeg_quality", default=90))
    apply_exif = bool(cfg.get("image", "apply_exif_transpose", default=True))
    rot = int(rotate_degrees or 0) % 360
    if rot not in (0, 90, 180, 270):
        rot = 0

    with Image.open(path) as im:
        if apply_exif:
            im = ImageOps.exif_transpose(im)
        if rot:
            # PIL rotate() is counter-clockwise; expand keeps the full page.
            im = im.rotate(rot, expand=True)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        long_edge = max(w, h)
        if max_dim and long_edge > max_dim:
            scale = max_dim / float(long_edge)
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()


MIME = "image/jpeg"
