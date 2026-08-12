"""Safety snapshots for destructive ``--force`` pipeline runs."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Set

from .config import Config

_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def _snapshot_destination(cfg: Config, directory: Path, source: Path) -> Path:
    """Preserve a useful logical path inside a snapshot directory."""
    try:
        return directory / "pages" / source.relative_to(cfg.pages_dir)
    except ValueError:
        pass
    try:
        return directory / "cases" / source.relative_to(cfg.cases_out_dir)
    except ValueError:
        return directory / "other" / source.name


def snapshot_json_files(
    cfg: Config, paths: Iterable[Path], reason: str
) -> Optional[Path]:
    """Copy existing affected JSON files into one timestamped directory.

    Returns ``None`` when none of the supplied paths currently exists. New
    outputs need no backup; only files that ``--force`` can overwrite are copied.
    """
    sources = sorted({
        Path(path) for path in paths
        if Path(path).is_file() and Path(path).suffix.lower() == ".json"
    })
    if not sources:
        return None

    tag = re.sub(r"[^a-z0-9_]+", "_", reason.lower()).strip("_") or "force"
    root = cfg.data_dir / "force_snapshots"
    stamp = datetime.now(timezone.utc).strftime(_STAMP_FORMAT)
    directory = root / f"{stamp}-{tag}"
    counter = 1
    while directory.exists():
        directory = root / f"{stamp}-{counter}-{tag}"
        counter += 1

    copied: List[dict] = []
    for source in sources:
        destination = _snapshot_destination(cfg, directory, source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append({"source": str(source), "snapshot": str(destination)})

    (directory / "snapshot_manifest.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "files": copied,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return directory


def case_output_paths(cfg: Config, case_ids: Iterable[str]) -> List[Path]:
    """Find existing per-case JSON records for stable segmentation case IDs."""
    wanted: Set[str] = set(case_ids)
    if not wanted or not cfg.cases_out_dir.is_dir():
        return []

    matches: List[Path] = []
    for path in cfg.cases_out_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("case_id") in wanted:
            matches.append(path)
    return sorted(matches)
