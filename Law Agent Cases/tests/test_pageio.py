"""Image preparation: EXIF transpose and Pass-A page rotation."""

from __future__ import annotations

import io

from PIL import Image

from court_pipeline.pageio import prepare_image_bytes


def _jpeg_path(tmp_path, size=(40, 20), color=(255, 0, 0)):
    """Write a small solid-color JPEG and return its path."""
    path = tmp_path / "page.jpg"
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


def test_prepare_image_applies_counterclockwise_rotation(pipeline_cfg, tmp_path):
    src = _jpeg_path(tmp_path, size=(40, 20))

    upright = prepare_image_bytes(src, pipeline_cfg, rotate_degrees=0)
    rotated = prepare_image_bytes(src, pipeline_cfg, rotate_degrees=90)

    with Image.open(io.BytesIO(upright)) as im:
        assert im.size == (40, 20)
    with Image.open(io.BytesIO(rotated)) as im:
        # 90° CCW turns a wide page into a tall one.
        assert im.size == (20, 40)


def test_prepare_image_ignores_invalid_rotation(pipeline_cfg, tmp_path):
    src = _jpeg_path(tmp_path, size=(40, 20))

    out = prepare_image_bytes(src, pipeline_cfg, rotate_degrees=45)
    with Image.open(io.BytesIO(out)) as im:
        assert im.size == (40, 20)
