"""Path repair after the project tree moves.

Regression suite for a bug that shipped and went unnoticed: `relocate-paths`
wrapped its entire body in ``if old_root != new_root``, and `page_cache_files`
was never repaired at all. When the tree moved but ``images_root`` did not, the
command reported success while every cache path stayed broken -- and because
``_page_verbatim_text`` returns "" for a missing file rather than raising,
consolidation would then emit empty transcripts and mark the cases done.

The invariant these tests defend: after `relocate-paths`, every path stored in
`cases.json` resolves.
"""

from __future__ import annotations

import json
from pathlib import Path

from court_pipeline.consolidate import page_issues
from court_pipeline.relocate_paths import relocate_stored_paths

from conftest import BOX


def _stale(cfg, filenames, root="/gone/old/tree"):
    return [f"{root}/data/pages/{BOX}/{f}.json" for f in filenames]


def _read_cases(cfg):
    return json.loads(cfg.cases_index_path.read_text())["cases"]


def test_cache_paths_are_repaired_when_the_image_root_is_unchanged(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    """The exact production failure: same image root, stale cache paths."""
    write_page("p1.jpg", order=0)
    write_page("p2.jpg", order=1)
    case = make_case("c1", ["p1.jpg", "p2.jpg"],
                     cache_files=_stale(pipeline_cfg, ["p1.jpg", "p2.jpg"]))
    write_cases_index([case])

    stats = relocate_stored_paths(pipeline_cfg)

    assert stats["old_root"] == stats["new_root"], "precondition: root did not change"
    assert stats["cases_index_cache_files_updated"] == 2
    repaired = _read_cases(pipeline_cfg)[0]["page_cache_files"]
    assert repaired == [
        str(pipeline_cfg.pages_dir / BOX / "p1.jpg.json"),
        str(pipeline_cfg.pages_dir / BOX / "p2.jpg.json"),
    ]
    assert all(Path(p).is_file() for p in repaired)


def test_repair_unblocks_consolidation(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    """End to end: broken paths look like missing transcriptions until repaired."""
    write_page("p1.jpg", order=0, verbatim_text="real transcript")
    case = make_case("c1", ["p1.jpg"], cache_files=_stale(pipeline_cfg, ["p1.jpg"]))
    write_cases_index([case])

    before = page_issues(pipeline_cfg, case)
    assert before and "missing" in before[0]

    relocate_stored_paths(pipeline_cfg)

    assert page_issues(pipeline_cfg, _read_cases(pipeline_cfg)[0]) == []


def test_repair_is_idempotent(pipeline_cfg, write_page, make_case, write_cases_index):
    write_page("p1.jpg", order=0)
    write_cases_index([make_case("c1", ["p1.jpg"],
                                 cache_files=_stale(pipeline_cfg, ["p1.jpg"]))])

    first = relocate_stored_paths(pipeline_cfg)
    second = relocate_stored_paths(pipeline_cfg)

    assert first["cases_index_cache_files_updated"] == 1
    assert second["cases_index_cache_files_updated"] == 0


def test_already_correct_paths_are_left_alone(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    write_page("p1.jpg", order=0)
    write_cases_index([make_case("c1", ["p1.jpg"])])
    before = _read_cases(pipeline_cfg)

    stats = relocate_stored_paths(pipeline_cfg)

    assert stats["cases_index_cache_files_updated"] == 0
    assert _read_cases(pipeline_cfg)[0]["page_cache_files"] == \
        before[0]["page_cache_files"]


def test_cache_paths_track_the_box_of_each_case(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    """Repair uses each case's own box, not a single global one."""
    write_page("a1.jpg", box="boxA", order=0)
    write_page("b1.jpg", box="boxB", order=0)
    write_cases_index([
        make_case("a", ["a1.jpg"], box="boxA", cache_files=["/gone/a1.jpg.json"]),
        make_case("b", ["b1.jpg"], box="boxB", cache_files=["/gone/b1.jpg.json"]),
    ])

    relocate_stored_paths(pipeline_cfg)

    cases = {c["case_id"]: c for c in _read_cases(pipeline_cfg)}
    assert cases["a"]["page_cache_files"] == [
        str(pipeline_cfg.pages_dir / "boxA" / "a1.jpg.json")]
    assert cases["b"]["page_cache_files"] == [
        str(pipeline_cfg.pages_dir / "boxB" / "b1.jpg.json")]


def test_a_page_list_that_grew_is_fully_rebuilt(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    """Length mismatches must not silently truncate the repair."""
    write_page("p1.jpg", order=0)
    write_page("p2.jpg", order=1)
    case = make_case("c1", ["p1.jpg", "p2.jpg"],
                     cache_files=["/gone/only-one.json"])
    write_cases_index([case])

    relocate_stored_paths(pipeline_cfg)

    assert len(_read_cases(pipeline_cfg)[0]["page_cache_files"]) == 2


def test_image_paths_are_rewritten_when_the_root_moves(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    """The original old-root -> new-root behavior still works."""
    old_root = "/previous/archive"
    pipeline_cfg.manifest_path.write_text(
        json.dumps({"images_root": old_root, "boxes": {}}), encoding="utf-8")
    write_page("p1.jpg", order=0)
    case = make_case("c1", ["p1.jpg"])
    case["page_paths"] = [f"{old_root}/{BOX}/p1.jpg"]
    case["cover_path"] = f"{old_root}/{BOX}/p1.jpg"
    write_cases_index([case])

    stats = relocate_stored_paths(pipeline_cfg)

    assert stats["old_root"] != stats["new_root"], "precondition: root changed"
    repaired = _read_cases(pipeline_cfg)[0]
    expected = str(pipeline_cfg.images_root / BOX / "p1.jpg")
    assert repaired["page_paths"] == [expected]
    assert repaired["cover_path"] == expected
    assert stats["cases_index_paths_updated"] == 2


def test_page_record_paths_are_rewritten_when_the_root_moves(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    old_root = "/previous/archive"
    pipeline_cfg.manifest_path.write_text(
        json.dumps({"images_root": old_root, "boxes": {}}), encoding="utf-8")
    page = write_page("p1.jpg", order=0, path=f"{old_root}/{BOX}/p1.jpg")
    write_cases_index([make_case("c1", ["p1.jpg"])])

    stats = relocate_stored_paths(pipeline_cfg)

    assert stats["page_records_updated"] == 1
    assert json.loads(page.read_text())["path"] == \
        str(pipeline_cfg.images_root / BOX / "p1.jpg")


def test_results_json_archive_root_is_rewritten(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    old_root = "/previous/archive"
    pipeline_cfg.manifest_path.write_text(
        json.dumps({"images_root": old_root, "boxes": {}}), encoding="utf-8")
    results = pipeline_cfg.output_dir / "results.json"
    results.write_text(json.dumps({"archive_root": old_root, "cases": []}), encoding="utf-8")
    write_page("p1.jpg", order=0)
    write_cases_index([make_case("c1", ["p1.jpg"])])

    relocate_stored_paths(pipeline_cfg)

    assert json.loads(results.read_text())["archive_root"] == str(pipeline_cfg.images_root)


def test_missing_cases_index_is_not_an_error(pipeline_cfg):
    assert not pipeline_cfg.cases_index_path.exists()

    stats = relocate_stored_paths(pipeline_cfg)

    assert stats["cases_index_cache_files_updated"] == 0
