"""Snapshots of results.json before anything overwrites it.

The backup is the only undo available for human corrections, and it has to hold
the state from *before* the write that triggered it. It also has to be
unobtrusive: a failing backup must never stop a save, because losing the ability
to save is worse than losing the undo buffer.
"""

from __future__ import annotations

import json

from court_viewer import db
from court_viewer.backup import (
    backup_results,
    list_backups,
    prune_backups,
    safe_backup_results,
)
from court_viewer.build_results import build_results
from court_viewer.config import Config as ViewerConfig

from conftest import BOX


def _seed_results(cfg, marker="original"):
    cfg.results_json.parent.mkdir(parents=True, exist_ok=True)
    cfg.results_json.write_text(json.dumps({
        "schema_version": 1,
        "cases": [{"case_id": "c1", "box": BOX,
                   "fields": {"plaintiff": {"gemini": None, "claude": None,
                                            "edited": marker}}}],
    }), encoding="utf-8")


def _edited(path, case_id="c1"):
    doc = json.loads(path.read_text())
    case = next(c for c in doc["cases"] if c["case_id"] == case_id)
    return case["fields"]["plaintiff"]["edited"]


def test_snapshot_is_written_under_the_local_root(viewer_cfg, isolated_local_root):
    _seed_results(viewer_cfg)

    snapshot = backup_results(viewer_cfg, "build")

    assert snapshot is not None and snapshot.is_file()
    assert isolated_local_root in snapshot.parents
    assert "CloudStorage" not in str(snapshot)


def test_snapshot_captures_content_and_reason(viewer_cfg):
    _seed_results(viewer_cfg, marker="before")

    snapshot = backup_results(viewer_cfg, "export")

    assert _edited(snapshot) == "before"
    assert snapshot.name.endswith("-export.json")
    assert snapshot.name.startswith("results-")


def test_no_snapshot_when_there_is_nothing_to_back_up(viewer_cfg):
    assert not viewer_cfg.results_json.exists()

    assert backup_results(viewer_cfg, "build") is None


def test_backups_can_be_disabled(viewer_cfg):
    _seed_results(viewer_cfg)
    viewer_cfg._data["backup"]["keep"] = 0

    assert backup_results(viewer_cfg, "build") is None
    assert list_backups(viewer_cfg) == []


def test_rapid_snapshots_do_not_overwrite_each_other(viewer_cfg):
    """Several saves inside one second must each keep their own copy."""
    _seed_results(viewer_cfg)

    paths = {backup_results(viewer_cfg, "export") for _ in range(4)}

    assert len(paths) == 4
    assert len(list_backups(viewer_cfg)) == 4


def test_rotation_keeps_the_configured_number(viewer_cfg):
    _seed_results(viewer_cfg)
    viewer_cfg._data["backup"]["keep"] = 3

    for _ in range(8):
        backup_results(viewer_cfg, "export")

    assert len(list_backups(viewer_cfg)) == 3


def test_prune_is_a_no_op_when_under_the_limit(viewer_cfg):
    _seed_results(viewer_cfg)
    backup_results(viewer_cfg, "build")

    assert prune_backups(viewer_cfg) == 0
    assert len(list_backups(viewer_cfg)) == 1


def test_unrelated_files_are_not_listed_or_pruned(viewer_cfg):
    _seed_results(viewer_cfg)
    backup_results(viewer_cfg, "build")
    stray = viewer_cfg.backups_dir / "notes.txt"
    stray.write_text("keep me", encoding="utf-8")

    prune_backups(viewer_cfg, keep=1)

    assert stray.is_file()
    assert all(p.suffix == ".json" for p in list_backups(viewer_cfg))


def test_backup_failure_never_blocks_the_caller(viewer_cfg, capsys):
    _seed_results(viewer_cfg)
    broken = ViewerConfig(
        {"paths": {"results_json": str(viewer_cfg.results_json),
                   "backups": "/proc/cannot/write/here"}},
        viewer_cfg.config_path,
    )

    assert safe_backup_results(broken, "build") is None
    assert "WARNING" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# wired into the two write paths
# ---------------------------------------------------------------------------
def test_build_results_snapshots_the_previous_file(
    viewer_cfg, write_pipeline_case, write_page
):
    write_page("p1.jpg", order=0)
    write_pipeline_case("c1", source_images=["p1.jpg"], plaintiff="machine")
    _seed_results(viewer_cfg, marker="human edit")

    build_results(viewer_cfg)

    snapshots = list_backups(viewer_cfg)
    assert len(snapshots) == 1
    assert _edited(snapshots[-1]) == "human edit"
    # The merge itself preserved the edit too; the snapshot is the safety net.
    assert _edited(viewer_cfg.results_json) == "human edit"


def test_export_snapshots_before_writing(viewer_cfg):
    _seed_results(viewer_cfg, marker="state before export")
    db.rebuild_from_results(viewer_cfg)

    db.export_results(viewer_cfg)

    snapshots = list_backups(viewer_cfg)
    assert len(snapshots) == 1
    assert _edited(snapshots[-1]) == "state before export"
    assert snapshots[-1].name.endswith("-export.json")


def test_snapshot_holds_the_pre_edit_state_after_a_save(viewer_cfg):
    """The undo case: edit a case, then recover the value from the snapshot."""
    _seed_results(viewer_cfg, marker="original value")
    db.rebuild_from_results(viewer_cfg)

    conn = db.connect(viewer_cfg.db_path)
    db.update_case(conn, "c1", {"fields": {"plaintiff": "overwritten"}})
    conn.close()
    db.export_results(viewer_cfg)

    assert _edited(viewer_cfg.results_json) == "overwritten"
    assert _edited(list_backups(viewer_cfg)[-1]) == "original value"
