"""Edit preservation across a pipeline re-run.

``results.json`` is the only copy of every human correction in the project, and
``build_results`` rewrites it in full each time the pipeline is re-run. The
contract it has to keep: refresh everything the machine produced, touch nothing
the human produced. These tests exist because a regression here is silent and
unrecoverable -- the edits are simply gone from the next write.
"""

from __future__ import annotations

import json

from court_viewer.build_results import build_results
from court_viewer.viewer_schema import FIELD_NAMES, effective, empty_trivalue

from conftest import BOX


def _write_results(cfg, cases):
    cfg.results_json.parent.mkdir(parents=True, exist_ok=True)
    cfg.results_json.write_text(
        json.dumps({"schema_version": 1, "cases": cases}), encoding="utf-8"
    )


def _case(doc, case_id):
    return next(c for c in doc["cases"] if c["case_id"] == case_id)


def _tri(gemini=None, claude=None, edited=None):
    return {"gemini": gemini, "claude": claude, "edited": edited}


# ---------------------------------------------------------------------------
# baseline construction
# ---------------------------------------------------------------------------
def test_builds_trivalues_from_pipeline_output(
    viewer_cfg, write_pipeline_case, write_page
):
    write_page("p1.jpg", order=0, verbatim_text="transcript one")
    write_pipeline_case("c1", source_images=["p1.jpg"], plaintiff="Smith")

    doc = build_results(viewer_cfg)

    case = _case(doc, "c1")
    assert case["fields"]["plaintiff"] == _tri(gemini="Smith")
    assert case["pages"][0]["transcript"] == _tri(gemini="transcript one")
    assert set(case["fields"]) == set(FIELD_NAMES)


def test_missing_page_record_yields_a_null_transcript(
    viewer_cfg, write_pipeline_case
):
    write_pipeline_case("c1", source_images=["ghost.jpg"])

    doc = build_results(viewer_cfg)

    assert _case(doc, "c1")["pages"][0]["transcript"]["gemini"] is None


def test_pipeline_claude_alternate_populates_page_slot(
    viewer_cfg, write_pipeline_case, write_page
):
    write_page(
        "p1.jpg",
        verbatim_text="Gemini baseline",
        transcription_alternates={
            "claude": {
                "verbatim_text": "Claude alternate",
                "error": None,
                "model": "claude-opus-test",
            }
        },
    )
    write_pipeline_case("c1", source_images=["p1.jpg"])

    transcript = _case(build_results(viewer_cfg), "c1")["pages"][0]["transcript"]

    assert transcript == _tri(
        gemini="Gemini baseline", claude="Claude alternate"
    )


def test_fresh_pipeline_claude_alternate_replaces_stale_slot(
    viewer_cfg, write_pipeline_case, write_page
):
    write_page(
        "p1.jpg",
        transcription_alternates={
            "claude": {"verbatim_text": "new alternate", "error": None}
        },
    )
    write_pipeline_case("c1", source_images=["p1.jpg"])
    _write_results(
        viewer_cfg,
        [{
            "case_id": "c1",
            "box": BOX,
            "pages": [{
                "filename": "p1.jpg",
                "transcript": _tri(claude="stale alternate"),
            }],
        }],
    )

    transcript = _case(build_results(viewer_cfg), "c1")["pages"][0]["transcript"]

    assert transcript["claude"] == "new alternate"


# ---------------------------------------------------------------------------
# preservation of human data
# ---------------------------------------------------------------------------
def test_field_edit_survives_a_rerun(viewer_cfg, write_pipeline_case, write_page):
    write_page("p1.jpg", order=0)
    write_pipeline_case("c1", source_images=["p1.jpg"], plaintiff="Smyth")
    _write_results(viewer_cfg, [{
        "case_id": "c1", "box": BOX,
        "fields": {"plaintiff": _tri(gemini="Smyth", edited="Smith")},
    }])

    doc = build_results(viewer_cfg)

    plaintiff = _case(doc, "c1")["fields"]["plaintiff"]
    assert plaintiff["edited"] == "Smith"
    assert effective(plaintiff) == "Smith"


def test_gemini_slot_is_refreshed_while_edit_is_kept(
    viewer_cfg, write_pipeline_case, write_page
):
    """A better machine value must land in `gemini` without disturbing `edited`."""
    write_page("p1.jpg", order=0)
    write_pipeline_case("c1", source_images=["p1.jpg"], plaintiff="NEW MACHINE VALUE")
    _write_results(viewer_cfg, [{
        "case_id": "c1", "box": BOX,
        "fields": {"plaintiff": _tri(gemini="old machine value", edited="human value")},
    }])

    plaintiff = _case(build_results(viewer_cfg), "c1")["fields"]["plaintiff"]

    assert plaintiff["gemini"] == "NEW MACHINE VALUE"
    assert plaintiff["edited"] == "human value"


def test_claude_alternate_is_preserved(viewer_cfg, write_pipeline_case, write_page):
    """Nothing populates `claude` yet, but the merge must not drop it if set."""
    write_page("p1.jpg", order=0)
    write_pipeline_case("c1", source_images=["p1.jpg"], plaintiff="Smith")
    _write_results(viewer_cfg, [{
        "case_id": "c1", "box": BOX,
        "fields": {"plaintiff": _tri(gemini="Smith", claude="Smythe")},
    }])

    assert _case(build_results(viewer_cfg), "c1")["fields"]["plaintiff"]["claude"] == "Smythe"


def test_page_transcript_edit_survives_and_is_matched_by_filename(
    viewer_cfg, write_pipeline_case, write_page
):
    write_page("p1.jpg", order=0, verbatim_text="machine A")
    write_page("p2.jpg", order=1, verbatim_text="machine B")
    write_pipeline_case("c1", source_images=["p1.jpg", "p2.jpg"])
    _write_results(viewer_cfg, [{
        "case_id": "c1", "box": BOX,
        # Deliberately in the opposite order: matching is by filename, not index.
        "pages": [
            {"filename": "p2.jpg", "transcript": _tri(edited="human B")},
            {"filename": "p1.jpg", "transcript": _tri(edited="human A")},
        ],
    }])

    pages = {p["filename"]: p for p in _case(build_results(viewer_cfg), "c1")["pages"]}

    assert pages["p1.jpg"]["transcript"]["edited"] == "human A"
    assert pages["p1.jpg"]["transcript"]["gemini"] == "machine A"
    assert pages["p2.jpg"]["transcript"]["edited"] == "human B"
    assert pages["p2.jpg"]["transcript"]["gemini"] == "machine B"


def test_review_status_and_notes_survive(viewer_cfg, write_pipeline_case, write_page):
    write_page("p1.jpg", order=0)
    write_pipeline_case("c1", source_images=["p1.jpg"])
    _write_results(viewer_cfg, [{
        "case_id": "c1", "box": BOX,
        "review_status": "verified",
        "notes": {"gemini": None, "claude": None, "edited": "checked against register"},
    }])

    case = _case(build_results(viewer_cfg), "c1")

    assert case["review_status"] == "verified"
    assert case["notes"]["edited"] == "checked against register"


def test_pipeline_never_fills_notes_gemini(viewer_cfg, write_pipeline_case, write_page):
    """Notes are user-owned; a stray machine value must not survive the merge."""
    write_page("p1.jpg", order=0)
    write_pipeline_case("c1", source_images=["p1.jpg"])
    _write_results(viewer_cfg, [{
        "case_id": "c1", "box": BOX,
        "notes": {"gemini": "machine wrote here", "claude": None, "edited": "mine"},
    }])

    notes = _case(build_results(viewer_cfg), "c1")["notes"]

    assert notes["gemini"] is None
    assert notes["edited"] == "mine"


def test_case_missing_from_pipeline_output_is_retained(
    viewer_cfg, write_pipeline_case, write_page
):
    """A vanished pipeline case must not take the human's work with it."""
    write_page("p1.jpg", order=0)
    write_pipeline_case("c1", source_images=["p1.jpg"])
    _write_results(viewer_cfg, [
        {"case_id": "c1", "box": BOX},
        {"case_id": "orphan", "box": BOX,
         "fields": {"plaintiff": _tri(edited="only in results.json")},
         "review_status": "flagged"},
    ])

    doc = build_results(viewer_cfg)

    orphan = _case(doc, "orphan")
    assert orphan["fields"]["plaintiff"]["edited"] == "only in results.json"
    assert orphan["review_status"] == "flagged"


def test_edits_survive_repeated_rebuilds(viewer_cfg, write_pipeline_case, write_page):
    """Idempotence: the fifth rebuild must be as safe as the first."""
    write_page("p1.jpg", order=0, verbatim_text="machine")
    write_pipeline_case("c1", source_images=["p1.jpg"], plaintiff="machine")
    _write_results(viewer_cfg, [{
        "case_id": "c1", "box": BOX,
        "fields": {"plaintiff": _tri(edited="human")},
        "pages": [{"filename": "p1.jpg", "transcript": _tri(edited="human page")}],
        "review_status": "verified",
    }])

    for _ in range(5):
        doc = build_results(viewer_cfg)

    case = _case(doc, "c1")
    assert case["fields"]["plaintiff"]["edited"] == "human"
    assert case["pages"][0]["transcript"]["edited"] == "human page"
    assert case["review_status"] == "verified"


# ---------------------------------------------------------------------------
# refresh of machine data
# ---------------------------------------------------------------------------
def test_structural_metadata_is_refreshed_from_the_pipeline(
    viewer_cfg, write_pipeline_case, write_page
):
    write_page("p1.jpg", order=7, page_type="cover_regular")
    write_pipeline_case(
        "c1", source_images=["p1.jpg"], page_range=[7, 7], is_appeal=True,
        field_confidence={"plaintiff": "high"},
    )
    _write_results(viewer_cfg, [{
        "case_id": "c1", "box": BOX, "is_appeal": False, "page_range": [0, 0],
        "field_confidence": {"plaintiff": "stale"},
        "pages": [{"filename": "p1.jpg", "order": 0, "page_type": "other"}],
    }])

    case = _case(build_results(viewer_cfg), "c1")

    assert case["is_appeal"] is True
    assert case["page_range"] == [7, 7]
    assert case["field_confidence"] == {"plaintiff": "high"}
    assert case["pages"][0]["page_type"] == "cover_regular"
    assert case["pages"][0]["order"] == 7
    assert case["provenance"]["model"] == "mock-flash"


def test_a_page_added_by_resegmentation_appears_with_no_edits(
    viewer_cfg, write_pipeline_case, write_page
):
    write_page("p1.jpg", order=0, verbatim_text="A")
    write_page("p2.jpg", order=1, verbatim_text="B")
    write_pipeline_case("c1", source_images=["p1.jpg", "p2.jpg"])
    _write_results(viewer_cfg, [{
        "case_id": "c1", "box": BOX,
        "pages": [{"filename": "p1.jpg", "transcript": _tri(edited="kept")}],
    }])

    pages = {p["filename"]: p for p in _case(build_results(viewer_cfg), "c1")["pages"]}

    assert pages["p1.jpg"]["transcript"]["edited"] == "kept"
    assert pages["p2.jpg"]["transcript"] == _tri(gemini="B")


def test_output_is_written_and_sorted(viewer_cfg, write_pipeline_case, write_page):
    write_page("p1.jpg", order=0)
    write_pipeline_case("c2", source_images=["p1.jpg"], box="boxB")
    write_pipeline_case("c1", source_images=["p1.jpg"], box="boxA")

    doc = build_results(viewer_cfg)

    assert viewer_cfg.results_json.is_file()
    on_disk = json.loads(viewer_cfg.results_json.read_text())
    assert [c["case_id"] for c in on_disk["cases"]] == ["c1", "c2"]
    assert on_disk["archive_root"] == str(viewer_cfg.archive_root)
    assert doc["cases"] == on_disk["cases"]


def test_duplicate_pipeline_case_ids_are_ignored(
    viewer_cfg, write_pipeline_case, write_page, pipeline_cfg
):
    write_page("p1.jpg", order=0)
    write_pipeline_case("c1", source_images=["p1.jpg"], plaintiff="first")
    # A second file on disk claiming the same case_id.
    dup = pipeline_cfg.cases_out_dir / "zzz_duplicate.json"
    dup.write_text(json.dumps({
        "case_id": "c1", "box": BOX, "source_images": ["p1.jpg"], "plaintiff": "second",
    }), encoding="utf-8")

    doc = build_results(viewer_cfg)

    assert len([c for c in doc["cases"] if c["case_id"] == "c1"]) == 1


def test_empty_trivalue_helper_shape():
    assert empty_trivalue() == {"gemini": None, "claude": None, "edited": None}
