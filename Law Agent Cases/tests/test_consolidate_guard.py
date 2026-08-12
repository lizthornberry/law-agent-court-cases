"""The incomplete-page guard on consolidation.

``assemble_full_transcript`` omits pages with empty ``verbatim_text``, so without
a pre-flight check a page that failed transcription simply vanishes from the
assembled case: the record still writes, still gets a ``.done_`` marker, and
comes out short with nothing anywhere saying so. These tests pin down which page
states count as a gap and that a gap stops the case rather than shortening it.
"""

from __future__ import annotations

import json

from court_pipeline.consolidate import (
    assemble_full_transcript,
    page_issues,
    run_consolidate,
)


# ---------------------------------------------------------------------------
# which page states count as a gap
# ---------------------------------------------------------------------------
def test_healthy_case_has_no_issues(pipeline_cfg, write_page, make_case):
    write_page("p1.jpg", order=0)
    write_page("p2.jpg", order=1)

    assert page_issues(pipeline_cfg, make_case("c1", ["p1.jpg", "p2.jpg"])) == []


def test_transcribe_error_is_a_gap(pipeline_cfg, write_page, make_case):
    write_page("p1.jpg", transcribe_error="429 RESOURCE_EXHAUSTED", verbatim_text="")

    issues = page_issues(pipeline_cfg, make_case("c1", ["p1.jpg"]))

    assert len(issues) == 1
    assert "transcribe_error" in issues[0]
    assert "429" in issues[0]


def test_error_is_reported_even_when_stale_text_remains(
    pipeline_cfg, write_page, make_case
):
    """A failed retry leaves the previous transcript in place; still a gap."""
    write_page("p1.jpg", transcribe_error="500 backend error", verbatim_text="stale text")

    issues = page_issues(pipeline_cfg, make_case("c1", ["p1.jpg"]))

    assert len(issues) == 1
    assert "transcribe_error" in issues[0]


def test_untranscribed_page_is_a_gap(pipeline_cfg, write_page, make_case):
    write_page("p1.jpg", transcription_status="pending", verbatim_text="")

    issues = page_issues(pipeline_cfg, make_case("c1", ["p1.jpg"]))

    assert "not transcribed" in issues[0]
    assert "pending" in issues[0]


def test_empty_text_on_a_done_page_is_a_gap(pipeline_cfg, write_page, make_case):
    write_page("p1.jpg", verbatim_text="   \n  ")

    issues = page_issues(pipeline_cfg, make_case("c1", ["p1.jpg"]))

    assert "verbatim_text is empty" in issues[0]


def test_missing_cache_file_is_a_gap(pipeline_cfg, make_case):
    issues = page_issues(pipeline_cfg, make_case("c1", ["never_written.jpg"]))

    assert "missing" in issues[0]


def test_unreadable_cache_file_is_a_gap(pipeline_cfg, write_page, make_case):
    path = write_page("p1.jpg")
    path.write_text("{ not json", encoding="utf-8")

    issues = page_issues(pipeline_cfg, make_case("c1", ["p1.jpg"]))

    assert "unreadable" in issues[0]


def test_skip_type_pages_are_expected_to_be_empty(pipeline_cfg, write_page, make_case):
    """blank / box_photo carry no transcription by design and must not block."""
    write_page("p1.jpg", order=0)
    write_page("blank.jpg", order=1, page_type="blank",
               verbatim_text="", transcription_status="skipped")
    write_page("photo.jpg", order=2, page_type="box_photo",
               verbatim_text="", transcription_status="skipped")

    case = make_case("c1", ["p1.jpg", "blank.jpg", "photo.jpg"],
                     page_types=["hearing", "blank", "box_photo"])

    assert page_issues(pipeline_cfg, case) == []


def test_every_bad_page_is_reported_not_just_the_first(
    pipeline_cfg, write_page, make_case
):
    write_page("p1.jpg", order=0)
    write_page("p2.jpg", order=1, transcribe_error="boom", verbatim_text="")
    write_page("p3.jpg", order=2, verbatim_text="")
    write_page("p4.jpg", order=3, transcription_status="pending", verbatim_text="")

    issues = page_issues(pipeline_cfg, make_case("c1", ["p1.jpg", "p2.jpg", "p3.jpg", "p4.jpg"]))

    assert len(issues) == 3
    assert all("p1.jpg" not in i for i in issues)


# ---------------------------------------------------------------------------
# what the guard does to a run
# ---------------------------------------------------------------------------
def test_incomplete_cases_are_held_back(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    write_page("good.jpg", order=0)
    write_page("bad.jpg", order=1, transcribe_error="boom", verbatim_text="")
    write_cases_index([
        make_case("clean", ["good.jpg"]),
        make_case("broken", ["bad.jpg"]),
    ])

    stats = run_consolidate(pipeline_cfg)

    assert stats["skipped_incomplete"] == 1
    assert stats["queued"] == 1
    written = [json.loads(p.read_text())["case_id"]
               for p in pipeline_cfg.cases_out_dir.glob("*.json")]
    assert written == ["clean"]


def test_held_back_case_is_not_marked_done(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    """It must come back for another attempt once transcription is fixed."""
    write_page("bad.jpg", transcription_status="pending", verbatim_text="")
    write_cases_index([make_case("broken", ["bad.jpg"])])

    run_consolidate(pipeline_cfg)
    assert not list(pipeline_cfg.cases_out_dir.glob(".done_*"))

    # Transcription now succeeds; the case consolidates without --force.
    write_page("bad.jpg", verbatim_text="now transcribed")
    stats = run_consolidate(pipeline_cfg)

    assert stats["queued"] == 1
    assert stats["skipped_incomplete"] == 0
    assert list(pipeline_cfg.cases_out_dir.glob(".done_*"))


def test_report_lists_the_blocked_cases(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    write_page("bad.jpg", transcribe_error="429 quota", verbatim_text="")
    write_cases_index([make_case("broken", ["bad.jpg"])])

    stats = run_consolidate(pipeline_cfg)

    report = pipeline_cfg.output_dir / "incomplete_cases.json"
    assert report.is_file()
    assert stats["incomplete_report"] == str(report)
    doc = json.loads(report.read_text())
    assert doc["n_cases"] == 1
    assert doc["cases"][0]["case_id"] == "broken"
    assert "429 quota" in doc["cases"][0]["issues"][0]


def test_no_report_when_everything_is_healthy(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    write_page("good.jpg")
    write_cases_index([make_case("clean", ["good.jpg"])])

    stats = run_consolidate(pipeline_cfg)

    assert not (pipeline_cfg.output_dir / "incomplete_cases.json").exists()
    assert "incomplete_report" not in stats


def test_allow_incomplete_consolidates_and_records_the_gaps(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    write_page("good.jpg", order=0, verbatim_text="kept text")
    write_page("bad.jpg", order=1, transcribe_error="boom", verbatim_text="")
    write_cases_index([make_case("broken", ["good.jpg", "bad.jpg"])])

    stats = run_consolidate(pipeline_cfg, allow_incomplete=True)

    assert stats["skipped_incomplete"] == 0
    assert stats["queued"] == 1
    record = json.loads(next(pipeline_cfg.cases_out_dir.glob("*.json")).read_text())
    assert len(record["incomplete_pages"]) == 1
    assert "bad.jpg" in record["incomplete_pages"][0]
    # The transcript really is short -- that is the point of recording the gap.
    assert "kept text" in record["full_transcript"]
    assert record["full_transcript"].count("[") == 1


def test_clean_record_has_no_recorded_gaps(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    write_page("good.jpg", verbatim_text="text")
    write_cases_index([make_case("clean", ["good.jpg"])])

    run_consolidate(pipeline_cfg)

    record = json.loads(next(pipeline_cfg.cases_out_dir.glob("*.json")).read_text())
    assert record["incomplete_pages"] == []


def test_box_filter_and_done_skipping_still_work(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    write_page("a1.jpg", box="boxA")
    write_page("b1.jpg", box="boxB")
    write_cases_index([
        make_case("a", ["a1.jpg"], box="boxA"),
        make_case("b", ["b1.jpg"], box="boxB"),
    ])

    first = run_consolidate(pipeline_cfg, boxes=["boxA"])
    assert first["queued"] == 1

    second = run_consolidate(pipeline_cfg, boxes=["boxA"])
    assert second["queued"] == 0
    assert second["skipped_done"] == 1


# ---------------------------------------------------------------------------
# assembly rules the guard depends on
# ---------------------------------------------------------------------------
def test_transcript_assembly_order_and_headers(pipeline_cfg, write_page, make_case):
    write_page("p1.jpg", order=0, verbatim_text="first")
    write_page("p2.jpg", order=1, verbatim_text="second")

    text = assemble_full_transcript(pipeline_cfg, make_case("c1", ["p1.jpg", "p2.jpg"]))

    assert text == "[p1.jpg | hearing]\nfirst\n\n[p2.jpg | hearing]\nsecond"


def test_skip_type_pages_are_omitted_from_the_transcript(
    pipeline_cfg, write_page, make_case
):
    write_page("p1.jpg", order=0, verbatim_text="real")
    write_page("blank.jpg", order=1, page_type="blank",
               verbatim_text="", transcription_status="skipped")

    case = make_case("c1", ["p1.jpg", "blank.jpg"], page_types=["hearing", "blank"])

    assert assemble_full_transcript(pipeline_cfg, case) == "[p1.jpg | hearing]\nreal"


def test_legacy_single_pass_records_are_not_flagged_as_pending(
    pipeline_cfg, make_case
):
    """Pre-two-pass page caches lack transcription_status but carry real text.

    Classify/transcribe already migrate them on read. The consolidation guard
    must do the same, or a finished box looks entirely untranscribed.
    """
    path = pipeline_cfg.pages_dir / "box1" / "legacy.jpg.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "box": "box1",
        "filename": "legacy.jpg",
        "order": 0,
        "sha1": "abc",
        "page_type": "hearing",
        "verbatim_text": "legacy transcript text",
        "provider": "gemini",
        "model": "gemini-3.1-pro-preview",
        "processed_at": "2026-06-24T16:53:39-04:00",
        # Intentionally no classified_at / transcription_status keys.
    }), encoding="utf-8")

    case = make_case("c1", ["legacy.jpg"])

    assert page_issues(pipeline_cfg, case) == []
    assert "legacy transcript text" in assemble_full_transcript(pipeline_cfg, case)


def test_legacy_skip_type_without_status_is_still_skipped(
    pipeline_cfg, make_case
):
    path = pipeline_cfg.pages_dir / "box1" / "photo.jpg.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "box": "box1",
        "filename": "photo.jpg",
        "order": 0,
        "page_type": "box_photo",
        "verbatim_text": "",
        "processed_at": "2026-06-24T16:53:39-04:00",
        "model": "(auto)",
    }), encoding="utf-8")

    case = make_case("c1", ["photo.jpg"], page_types=["box_photo"])

    assert page_issues(pipeline_cfg, case) == []
    assert assemble_full_transcript(pipeline_cfg, case) == ""

