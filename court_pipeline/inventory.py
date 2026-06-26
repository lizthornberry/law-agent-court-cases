"""Incremental inventory: walk box subfolders, hash images, write manifest.json."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .config import Config
from .util import is_image, natural_key, now_iso, read_json, sha1_file, write_json


def _list_boxes(images_root: Path) -> List[Path]:
    return sorted(
        [p for p in images_root.iterdir() if p.is_dir()],
        key=lambda p: natural_key(p.name),
    )


def _list_images(box_dir: Path) -> List[Path]:
    return sorted(
        [p for p in box_dir.iterdir() if p.is_file() and is_image(p)],
        key=lambda p: natural_key(p.name),
    )


def build_manifest(cfg: Config) -> Dict[str, Any]:
    """Walk the image tree and (incrementally) build the manifest.

    Re-uses sha1 from the previous manifest when file size is unchanged, so
    re-running over a large, mostly-unchanged corpus is fast. Flags each image
    as `is_new` (no prior manifest entry or hash changed).
    """
    images_root = cfg.images_root
    if not images_root.exists():
        raise FileNotFoundError(f"images_root does not exist: {images_root}")

    prev: Dict[str, Dict[str, Any]] = {}
    if cfg.manifest_path.exists():
        old = read_json(cfg.manifest_path)
        for box, items in old.get("boxes", {}).items():
            for it in items:
                prev[f"{box}/{it['filename']}"] = it

    boxes: Dict[str, List[Dict[str, Any]]] = {}
    n_images = n_new = 0
    for box_dir in _list_boxes(images_root):
        box = box_dir.name
        items: List[Dict[str, Any]] = []
        for order, img in enumerate(_list_images(box_dir)):
            size = img.stat().st_size
            key = f"{box}/{img.name}"
            old_it = prev.get(key)
            if old_it and old_it.get("size") == size and old_it.get("sha1"):
                sha1 = old_it["sha1"]
                is_new = False
            else:
                sha1 = sha1_file(img)
                is_new = not (old_it and old_it.get("sha1") == sha1)
            items.append(
                {
                    "filename": img.name,
                    "path": str(img),
                    "order": order,
                    "size": size,
                    "sha1": sha1,
                    "is_first_in_box": order == 0,
                    "is_new": is_new,
                }
            )
            n_images += 1
            if is_new:
                n_new += 1
        if items:
            boxes[box] = items

    manifest = {
        "generated_at": now_iso(),
        "images_root": str(images_root),
        "n_boxes": len(boxes),
        "n_images": n_images,
        "n_new": n_new,
        "boxes": boxes,
    }
    cfg.ensure_dirs()
    write_json(cfg.manifest_path, manifest)
    return manifest


def load_manifest(cfg: Config) -> Dict[str, Any]:
    if not cfg.manifest_path.exists():
        raise FileNotFoundError(
            f"manifest not found at {cfg.manifest_path}; run the `inventory` command first"
        )
    return read_json(cfg.manifest_path)


def iter_images(manifest: Dict[str, Any], boxes: List[str] | None = None):
    """Yield (box, item) for every image, optionally filtered to given boxes."""
    for box, items in manifest.get("boxes", {}).items():
        if boxes and box not in boxes:
            continue
        for it in items:
            yield box, it
