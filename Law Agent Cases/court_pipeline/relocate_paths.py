"""Rewrite stale absolute paths after the project tree is moved.

Updates cached JSON that stores image paths (manifest, page cache, cases index,
results.json) so they point at the current ``images_root`` / ``archive_root``.

Two different roots are involved, which is why there are two repair strategies:

* Image paths (``path``, ``cover_path``, ``page_paths``, ``archive_root``) live
  under ``images_root`` and are rewritten old-root -> new-root.
* Page cache paths (``page_cache_files``) live under the pipeline's own
  ``data/pages`` directory, NOT under ``images_root``, so a root rewrite never
  matches them. They are instead recomputed from their logical key
  (``box`` + ``filename``), which is what ``segment`` derives them from anyway.
"""

from __future__ import annotations

from itertools import zip_longest
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


def _repair_cases_index(
    cfg: Config, old: Path, new_root: Path, stats: Dict[str, int]
) -> None:
    """Fix image paths and page cache paths in ``cases.json``.

    The cache-file repair runs even when the image root is unchanged: the project
    tree can move while ``images_root`` stays put (an archive stored outside the
    project, say), and a stale ``page_cache_files`` entry is worse than a stale
    image path -- consolidation reads transcripts through it, silently gets an
    empty string for every page, and produces an empty transcript with no error.
    """
    if not cfg.cases_index_path.exists():
        return
    index = read_json(cfg.cases_index_path)

    for case in index.get("cases") or []:
        if old != new_root:
            if case.get("cover_path"):
                new_path, changed = _rewrite_if_under(str(case["cover_path"]), old, new_root)
                if changed:
                    case["cover_path"] = new_path
                    stats["cases_index_paths_updated"] += 1
            updated_paths: List[str] = []
            for p in list(case.get("page_paths") or []):
                new_path, changed = _rewrite_if_under(str(p), old, new_root)
                if changed:
                    stats["cases_index_paths_updated"] += 1
                updated_paths.append(new_path)
            case["page_paths"] = updated_paths

        # Derived from the logical key, exactly as segment.py builds them.
        box = case.get("box") or ""
        expected = [
            str(cfg.pages_dir / box / f"{fname}.json")
            for fname in (case.get("page_files") or [])
        ]
        current = list(case.get("page_cache_files") or [])
        if expected != current:
            stats["cases_index_cache_files_updated"] += sum(
                1 for a, b in zip_longest(expected, current) if a != b
            )
            case["page_cache_files"] = expected

    write_json(cfg.cases_index_path, index)


def relocate_stored_paths(cfg: Config, old_root: Path | None = None) -> Dict[str, Any]:
    new_root = cfg.images_root.resolve()
    old = (old_root or _detect_old_root(cfg))
    if old is None:
        old = new_root
    old = old.resolve()

    stats: Dict[str, int] = {
        "page_records_updated": 0,
        "cases_index_paths_updated": 0,
        "cases_index_cache_files_updated": 0,
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

    _repair_cases_index(cfg, old, new_root, stats)

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
