"""Segmentation rules: where one case ends and the next begins.

Segmentation is pure Python over classified page records, with no LLM and no
network, so it is the cheapest part of the pipeline to pin down and the most
expensive to get wrong: a bad split silently merges two cases' pages into one
record, and every downstream transcript and extracted field inherits the error.
"""

from __future__ import annotations

import json
from pathlib import Path

from court_pipeline.segment import segment_cases

from conftest import BOX


def _ids(out):
    return [c["case_id"] for c in out["cases"]]


def _by_id(out):
    return {c["case_id"]: c for c in out["cases"]}


def test_cover_page_starts_a_case(pipeline_cfg, write_page):
    write_page("p1.jpg", order=0, page_type="cover_regular")
    write_page("p2.jpg", order=1, page_type="hearing")
    write_page("p3.jpg", order=2, page_type="cover_regular")
    write_page("p4.jpg", order=3, page_type="hearing")

    out = segment_cases(pipeline_cfg)

    assert out["n_cases"] == 2
    cases = out["cases"]
    assert cases[0]["page_files"] == ["p1.jpg", "p2.jpg"]
    assert cases[1]["page_files"] == ["p3.jpg", "p4.jpg"]
    assert [c["start_reason"] for c in cases] == ["cover", "cover"]


def test_pages_following_a_cover_attach_to_it(pipeline_cfg, write_page):
    write_page("p1.jpg", order=0, page_type="cover_regular")
    for i, name in enumerate(["p2.jpg", "p3.jpg", "p4.jpg"], start=1):
        write_page(name, order=i, page_type="hearing")

    out = segment_cases(pipeline_cfg)

    assert out["n_cases"] == 1
    assert out["cases"][0]["page_files"] == ["p1.jpg", "p2.jpg", "p3.jpg", "p4.jpg"]


def test_margin_case_number_change_splits(pipeline_cfg, write_page):
    write_page("p1.jpg", order=0, page_type="cover_regular", margin_case_number="12")
    write_page("p2.jpg", order=1, page_type="hearing", margin_case_number="12")
    write_page("p3.jpg", order=2, page_type="hearing", margin_case_number="13")

    out = segment_cases(pipeline_cfg)

    assert out["n_cases"] == 2
    assert out["cases"][1]["start_reason"] == "margin_change"
    assert out["cases"][1]["page_files"] == ["p3.jpg"]


def test_margin_number_is_normalized_before_comparison(pipeline_cfg, write_page):
    """'12', ' 12.' and '1 2' are the same number; punctuation must not split."""
    write_page("p1.jpg", order=0, page_type="cover_regular", margin_case_number="12")
    write_page("p2.jpg", order=1, page_type="hearing", margin_case_number=" 12. ")
    write_page("p3.jpg", order=2, page_type="hearing", margin_case_number="1 2")

    out = segment_cases(pipeline_cfg)

    assert out["n_cases"] == 1


def test_missing_margin_number_does_not_split(pipeline_cfg, write_page):
    """A page the model could not read a number off must not start a new case."""
    write_page("p1.jpg", order=0, page_type="cover_regular", margin_case_number="12")
    write_page("p2.jpg", order=1, page_type="hearing", margin_case_number=None)
    write_page("p3.jpg", order=2, page_type="hearing", margin_case_number="12")

    out = segment_cases(pipeline_cfg)

    assert out["n_cases"] == 1
    assert out["cases"][0]["page_files"] == ["p1.jpg", "p2.jpg", "p3.jpg"]


def test_margin_split_can_be_disabled(pipeline_cfg, write_page):
    pipeline_cfg._data["segment"]["split_on_margin_case_number_change"] = False
    write_page("p1.jpg", order=0, page_type="cover_regular", margin_case_number="12")
    write_page("p2.jpg", order=1, page_type="hearing", margin_case_number="13")

    out = segment_cases(pipeline_cfg)

    assert out["n_cases"] == 1


def test_box_boundary_starts_a_new_case_and_resets_numbering(pipeline_cfg, write_page):
    write_page("a1.jpg", box="boxA", order=0, page_type="cover_regular")
    write_page("b1.jpg", box="boxB", order=0, page_type="cover_regular")

    out = segment_cases(pipeline_cfg)

    assert out["n_cases"] == 2
    assert _ids(out) == ["boxA__001", "boxB__001"]


def test_pages_before_the_first_cover_still_form_a_case_flagged_for_review(
    pipeline_cfg, write_page
):
    """Nothing may be dropped just because the cover photo is missing."""
    write_page("orphan.jpg", order=0, page_type="hearing")
    write_page("cover.jpg", order=1, page_type="cover_regular")

    out = segment_cases(pipeline_cfg)

    cases = out["cases"]
    assert out["n_cases"] == 2
    assert cases[0]["page_files"] == ["orphan.jpg"]
    assert cases[0]["needs_review"] is True
    assert cases[0]["start_reason"] == "box_start"
    assert cases[1]["needs_review"] is False
    assert out["n_needs_review"] == 1


def test_cover_appeal_flags_the_case(pipeline_cfg, write_page):
    write_page("p1.jpg", order=0, page_type="cover_appeal")
    write_page("p2.jpg", order=1, page_type="hearing")

    out = segment_cases(pipeline_cfg)

    assert out["cases"][0]["is_appeal"] is True
    assert out["n_appeals"] == 1


def test_box_photo_contributes_nothing_but_resets_context(pipeline_cfg, write_page):
    write_page("box.jpg", order=0, page_type="box_photo")
    write_page("p1.jpg", order=1, page_type="cover_regular")

    out = segment_cases(pipeline_cfg)

    assert out["n_cases"] == 1
    assert "box.jpg" not in out["cases"][0]["page_files"]


def test_blank_pages_are_kept_in_the_case(pipeline_cfg, write_page):
    """Blank pages are not transcribed, but they are still part of the document."""
    write_page("p1.jpg", order=0, page_type="cover_regular")
    write_page("p2.jpg", order=1, page_type="blank", verbatim_text="")

    out = segment_cases(pipeline_cfg)

    assert out["cases"][0]["page_files"] == ["p1.jpg", "p2.jpg"]
    assert out["cases"][0]["page_types"] == ["cover_regular", "blank"]


def test_page_cache_files_are_derived_from_box_and_filename(pipeline_cfg, write_page):
    """cases.json must address page caches by logical key.

    This is the invariant relocate-paths repairs: page_cache_files has to equal
    pages_dir/<box>/<filename>.json, because consolidation reads every transcript
    through it and a stale entry yields an empty transcript with no error.
    """
    write_page("p1.jpg", order=0, page_type="cover_regular")
    write_page("p2.jpg", order=1, page_type="hearing")

    out = segment_cases(pipeline_cfg)

    case = out["cases"][0]
    expected = [
        str(pipeline_cfg.pages_dir / BOX / "p1.jpg.json"),
        str(pipeline_cfg.pages_dir / BOX / "p2.jpg.json"),
    ]
    assert case["page_cache_files"] == expected
    assert all(Path(p).is_file() for p in case["page_cache_files"])


def test_parallel_lists_stay_aligned(pipeline_cfg, write_page):
    """page_files / page_types / page_cache_files are zipped downstream."""
    for i, ptype in enumerate(["cover_regular", "hearing", "blank", "warrant"]):
        write_page(f"p{i}.jpg", order=i, page_type=ptype)

    case = segment_cases(pipeline_cfg)["cases"][0]

    n = len(case["page_files"])
    assert len(case["page_types"]) == n
    assert len(case["page_cache_files"]) == n
    assert len(case["page_paths"]) == n
    assert len(case["orders"]) == n


def test_page_range_and_cover_defaults(pipeline_cfg, write_page):
    write_page("p1.jpg", order=5, page_type="hearing")
    write_page("p2.jpg", order=9, page_type="hearing")

    case = segment_cases(pipeline_cfg)["cases"][0]

    assert case["page_range"] == [5, 9]
    # No cover was detected, so the first page stands in as the cover image.
    assert case["cover_filename"] == "p1.jpg"
    assert case["cover_path"] == case["page_paths"][0]


def test_provisional_case_number_is_taken_from_the_pages(pipeline_cfg, write_page):
    write_page("p1.jpg", order=0, page_type="cover_regular", margin_case_number=None)
    write_page("p2.jpg", order=1, page_type="hearing", margin_case_number="42.")

    case = segment_cases(pipeline_cfg)["cases"][0]

    assert case["provisional_case_number"] == "42"


def test_boxes_filter_restricts_output(pipeline_cfg, write_page):
    write_page("a1.jpg", box="boxA", order=0, page_type="cover_regular")
    write_page("b1.jpg", box="boxB", order=0, page_type="cover_regular")

    out = segment_cases(pipeline_cfg, boxes=["boxA"])

    assert _ids(out) == ["boxA__001"]


def test_segmentation_is_deterministic(pipeline_cfg, write_page):
    for i, ptype in enumerate(["cover_regular", "hearing", "cover_regular", "hearing"]):
        write_page(f"p{i}.jpg", order=i, page_type=ptype)

    first = segment_cases(pipeline_cfg)
    second = segment_cases(pipeline_cfg)

    assert first["cases"] == second["cases"]


def test_index_is_written_to_disk(pipeline_cfg, write_page):
    write_page("p1.jpg", order=0, page_type="cover_regular")

    out = segment_cases(pipeline_cfg)

    assert pipeline_cfg.cases_index_path.is_file()
    on_disk = json.loads(pipeline_cfg.cases_index_path.read_text())
    assert on_disk["cases"] == out["cases"]
