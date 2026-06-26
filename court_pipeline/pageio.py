"""Image preparation: EXIF orientation, downscale, re-encode to JPEG bytes."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

from .config import Config


def prepare_image_bytes(path: Path, cfg: Config) -> bytes:
    """Load an image, apply EXIF orientation, downscale, return JPEG bytes.

    Downscaling the long edge to `image.max_dimension` keeps token cost down
    while preserving enough detail for handwriting.
    """
    max_dim = int(cfg.get("image", "max_dimension", default=2200))
    quality = int(cfg.get("image", "jpeg_quality", default=90))
    apply_exif = bool(cfg.get("image", "apply_exif_transpose", default=True))

    with Image.open(path) as im:
        if apply_exif:
            im = ImageOps.exif_transpose(im)
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
