"""Rewrite stale absolute paths after the project tree is moved.

Updates cached JSON that stores image paths (manifest, page cache, cases index,
results.json) so they point at the current ``images_root`` / ``archive_root``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config, load_config
from .inventory import build_manifest, rewrite_path_under_root
from .util import read_json, write_json


def _detect_old_root(cfg: Config) -> Optional[Path]:
    if cfg.manifest_path.exists():
        root = read_json(cfg.manifest_path).get("images_root")
        if root:
            return Path(root).expanduser().resolve()
    return None


def _rewrite_if_under(path_str: str, old_root: Path, new_root: Path) -> tuple[str, bool]:
    rewritten = rewrite_path_under_root(path_str, old_root, new_root)
    return rewritten, rewritten != path_str


def relocate_stored_paths(cfg: Config, old_root: Path | None = None) -> Dict[str, Any]:
    new_root = cfg.images_root.resolve()
    old = (old_root or _detect_old_root(cfg))
    if old is None:
        old = new_root
    old = old.resolve()

    stats: Dict[str, int] = {
        "page_records_updated": 0,
        "cases_index_paths_updated": 0,
        "results_json_updated": 0,
    }

    if old != new_root:
        for page_json in cfg.pages_dir.rglob("*.json"):
            data = read_json(page_json)
            if not isinstance(data, dict) or "path" not in data:
                continue
            new_path, changed = _rewrite_if_under(str(data["path"]), old, new_root)
            if changed:
                data["path"] = new_path
                write_json(page_json, data)
                stats["page_records_updated"] += 1

        if cfg.cases_index_path.exists():
            index = read_json(cfg.cases_index_path)
            for case in index.get("cases") or []:
                if case.get("cover_path"):
                    new_path, changed = _rewrite_if_under(str(case["cover_path"]), old, new_root)
                    if changed:
                        case["cover_path"] = new_path
                        stats["cases_index_paths_updated"] += 1
                page_paths: List[str] = list(case.get("page_paths") or [])
                updated_paths: List[str] = []
                for p in page_paths:
                    new_path, changed = _rewrite_if_under(str(p), old, new_root)
                    if changed:
                        stats["cases_index_paths_updated"] += 1
                    updated_paths.append(new_path)
                case["page_paths"] = updated_paths

            write_json(cfg.cases_index_path, index)

        viewer_results = cfg.base_dir.parent / "court_viewer" / "sample_results.json"
        for results_path in (
            cfg.output_dir / "results.json",
            viewer_results,
        ):
            if not results_path.is_file():
                continue
            doc = read_json(results_path)
            if not isinstance(doc, dict):
                continue
            archive = doc.get("archive_root")
            if archive:
                new_archive, changed = _rewrite_if_under(str(archive), old, new_root)
                if changed:
                    doc["archive_root"] = new_archive
                    write_json(results_path, doc)
                    stats["results_json_updated"] += 1

    manifest = build_manifest(cfg)
    stats["manifest_images"] = manifest["n_images"]
    stats["old_root"] = str(old)
    stats["new_root"] = str(new_root)
    return stats


def main() -> int:
    cfg = load_config()
    stats = relocate_stored_paths(cfg)
    print("Relocated stored paths:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
