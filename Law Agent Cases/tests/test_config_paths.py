"""Which paths belong in the synced tree and which must stay out of it.

SQLite runs in WAL mode, so the live database is a ``.db`` plus its ``-wal`` and
``-shm`` companions. A sync client uploads those independently and can restore a
``.db`` without the WAL that completes it. Everything ephemeral therefore
resolves under the local root; everything canonical and portable stays with the
project tree. The shipped config is asserted directly, because that is the file
that actually decides where the real database lands.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from court_viewer.config import Config as ViewerConfig, load_config, local_root

PKG_DIR = Path(__file__).resolve().parent.parent / "court_viewer"


def _cfg(paths, base=None):
    return ViewerConfig({"paths": paths}, (base or PKG_DIR) / "config.yaml")


# ---------------------------------------------------------------------------
# resolution rules
# ---------------------------------------------------------------------------
def test_ephemeral_paths_resolve_under_the_local_root(isolated_local_root):
    cfg = _cfg({"db": "viewer.db", "thumbnails": "thumbnails", "backups": "backups"})

    assert cfg.db_path == isolated_local_root / "viewer.db"
    assert cfg.thumbnails_dir == isolated_local_root / "thumbnails"
    assert cfg.backups_dir == isolated_local_root / "backups"


def test_canonical_paths_resolve_against_the_project_tree(tmp_path):
    base = tmp_path / "proj"
    base.mkdir()
    cfg = _cfg({"results_json": "../out/results.json", "archive_root": "../images"},
               base=base)

    assert cfg.results_json == (tmp_path / "out" / "results.json").resolve()
    assert cfg.archive_root == (tmp_path / "images").resolve()


def test_absolute_paths_win_over_both_roots(tmp_path):
    explicit = tmp_path / "elsewhere" / "custom.db"
    cfg = _cfg({"db": str(explicit)})

    assert cfg.db_path == explicit


def test_local_root_follows_the_environment_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("COURT_VIEWER_HOME", str(tmp_path / "custom_home"))

    assert local_root() == (tmp_path / "custom_home").resolve()


def test_local_root_defaults_under_the_home_directory(monkeypatch):
    monkeypatch.delenv("COURT_VIEWER_HOME", raising=False)

    root = local_root()

    assert root == Path.home() / ".court-viewer"
    assert root.parent == Path.home()


def test_defaults_apply_when_config_omits_the_keys(isolated_local_root):
    cfg = ViewerConfig({}, PKG_DIR / "config.yaml")

    assert cfg.db_path == isolated_local_root / "viewer.db"
    assert cfg.thumbnails_dir == isolated_local_root / "thumbnails"
    assert cfg.backups_dir == isolated_local_root / "backups"
    assert cfg.backup_keep == 20


# ---------------------------------------------------------------------------
# the configs that actually ship
# ---------------------------------------------------------------------------
def _shipped(name):
    path = PKG_DIR / name
    return ViewerConfig(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, path)


def test_shipped_config_keeps_live_state_out_of_the_synced_tree(monkeypatch):
    """Guards the real config.yaml, not just the resolution helper."""
    monkeypatch.delenv("COURT_VIEWER_HOME", raising=False)
    cfg = _shipped("config.yaml")
    project_root = PKG_DIR.parent.parent

    for path in (cfg.db_path, cfg.thumbnails_dir, cfg.backups_dir):
        assert project_root not in path.parents, f"{path} is inside the project tree"
        assert "CloudStorage" not in str(path)


def test_shipped_config_keeps_results_json_portable(monkeypatch):
    monkeypatch.delenv("COURT_VIEWER_HOME", raising=False)
    cfg = _shipped("config.yaml")
    project_root = PKG_DIR.parent.parent

    assert project_root in cfg.results_json.parents
    assert cfg.results_json.name == "results.json"


def test_test_config_does_not_collide_with_the_production_one(monkeypatch):
    """Both resolve under one local root, so their filenames must differ."""
    monkeypatch.delenv("COURT_VIEWER_HOME", raising=False)
    production = _shipped("config.yaml")
    batch_test = _shipped("config.batch-test.yaml")

    assert production.db_path != batch_test.db_path
    assert production.thumbnails_dir != batch_test.thumbnails_dir
    assert production.backups_dir != batch_test.backups_dir
    assert production.results_json != batch_test.results_json


def test_load_config_reads_the_shipped_file():
    cfg = load_config()

    assert cfg.config_path == (PKG_DIR / "config.yaml")
    assert cfg.backup_keep > 0
