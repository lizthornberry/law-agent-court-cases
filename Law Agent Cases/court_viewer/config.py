"""Configuration loader for court_viewer.

Reads ``court_viewer/config.yaml`` and resolves all paths relative to the config
file's directory (so the viewer is portable across machines/checkouts).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml

PKG_DIR = Path(__file__).resolve().parent


class Config:
    """Thin wrapper around the parsed config.yaml with resolved paths."""

    def __init__(self, data: Dict[str, Any], config_path: Path):
        self._data = data
        self.config_path = config_path
        self.base_dir = config_path.parent

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def resolve(self, rel: str) -> Path:
        p = Path(rel).expanduser()
        if not p.is_absolute():
            p = (self.base_dir / p).resolve()
        return p

    # -- resolved paths -------------------------------------------------
    @property
    def results_json(self) -> Path:
        return self.resolve(self.get("paths", "results_json", default="../court_pipeline/output/results.json"))

    @property
    def db_path(self) -> Path:
        return self.resolve(self.get("paths", "db", default="viewer.db"))

    @property
    def archive_root(self) -> Path:
        return self.resolve(self.get("paths", "archive_root", default="../Law Agent Civil Cases"))

    @property
    def thumbnails_dir(self) -> Path:
        return self.resolve(self.get("paths", "thumbnails", default="thumbnails"))

    @property
    def pipeline_cases_dir(self) -> Path:
        return self.resolve(self.get("paths", "pipeline_cases_dir", default="../court_pipeline/output/cases"))

    @property
    def pipeline_pages_dir(self) -> Path:
        return self.resolve(self.get("paths", "pipeline_pages_dir", default="../court_pipeline/data/pages"))

    # -- thumbnail settings --------------------------------------------
    @property
    def thumb_max_dimension(self) -> int:
        return int(self.get("thumbnail", "max_dimension", default=400))

    @property
    def thumb_jpeg_quality(self) -> int:
        return int(self.get("thumbnail", "jpeg_quality", default=80))

    # -- server --------------------------------------------------------
    @property
    def host(self) -> str:
        return str(self.get("server", "host", default="127.0.0.1"))

    @property
    def port(self) -> int:
        return int(self.get("server", "port", default=8000))


def load_config(config_path: "str | os.PathLike | None" = None) -> Config:
    """Load config.yaml.

    Resolution order: explicit ``config_path`` arg, then the ``COURT_VIEWER_CONFIG``
    environment variable, then the default ``config.yaml`` next to this package.
    """
    if config_path is None:
        config_path = os.environ.get("COURT_VIEWER_CONFIG") or None
    path = Path(config_path).resolve() if config_path else (PKG_DIR / "config.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Config(data, path)
