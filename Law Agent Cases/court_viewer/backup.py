"""Snapshot ``results.json`` before anything overwrites it.

``results.json`` is the only copy of every human correction, note and review
status in the project. Two code paths rewrite it wholesale -- ``build_results``
(merge from a pipeline re-run) and ``db.export_results`` (write-back after an
edit) -- and neither is reversible. A bad merge, a half-synced OneDrive copy or a
mistaken ``--force`` upstream would otherwise take the edits with it.

Snapshots go under the local state root (``~/.court-viewer/backups`` by default),
not the synced tree: they are a local undo buffer, and dozens of multi-megabyte
JSON copies are exactly the kind of churn a sync client handles badly. The
offsite copy of the data is ``results.json`` itself, which OneDrive already syncs.

Backups are best-effort by design. A failure here must never block a save, so
every entry point swallows its errors and warns instead.
"""

from __future__ import annotations

import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .config import Config

_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_BACKUP_RE = re.compile(r"^results-\d{8}T\d{6}Z(-\d+)?-[a-z0-9_]+\.json$")


def list_backups(config: Config) -> List[Path]:
    """Existing snapshots, oldest first (timestamps sort lexicographically)."""
    directory = config.backups_dir
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("results-*.json") if _BACKUP_RE.match(p.name))


def prune_backups(config: Config, keep: Optional[int] = None) -> int:
    """Delete all but the newest ``keep`` snapshots. Returns the number removed."""
    limit = config.backup_keep if keep is None else keep
    if limit <= 0:
        return 0
    existing = list_backups(config)
    removed = 0
    for path in existing[: max(0, len(existing) - limit)]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def backup_results(
    config: Config, reason: str, source: Optional[Path] = None
) -> Optional[Path]:
    """Copy the current ``results.json`` aside before it is overwritten.

    ``reason`` is a short tag recorded in the filename (e.g. ``build``, ``export``)
    so it is obvious afterwards which code path was about to run. ``source``
    overrides the configured results path for callers writing somewhere else.
    Returns the snapshot path, or ``None`` when there was nothing to back up or
    backups are disabled (``backup.keep: 0``).
    """
    source = source or config.results_json
    if config.backup_keep <= 0 or not source.is_file():
        return None

    tag = re.sub(r"[^a-z0-9_]+", "_", reason.lower()).strip("_") or "manual"
    directory = config.backups_dir
    directory.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime(_STAMP_FORMAT)
    dest = directory / f"results-{stamp}-{tag}.json"
    # Several saves can land inside the same second; keep each one.
    counter = 1
    while dest.exists():
        dest = directory / f"results-{stamp}-{counter}-{tag}.json"
        counter += 1

    shutil.copy2(source, dest)
    prune_backups(config)
    return dest


def safe_backup_results(
    config: Config, reason: str, source: Optional[Path] = None
) -> Optional[Path]:
    """:func:`backup_results` that warns instead of raising."""
    try:
        return backup_results(config, reason, source=source)
    except Exception as exc:  # noqa: BLE001 - a backup must never break a save
        print(f"WARNING: could not back up results.json ({exc})", file=sys.stderr)
        return None
