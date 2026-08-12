"""Forced pipeline rewrites retain the JSON they are about to replace."""

from __future__ import annotations

import json
from pathlib import Path

from court_pipeline.classify_transcribe import run_classify, run_transcribe
from court_pipeline.consolidate import run_consolidate


def _item(record_path: Path, *, first: bool = False) -> dict:
    record = json.loads(record_path.read_text())
    return {
        "filename": record["filename"],
        "path": record["path"],
        "order": record["order"],
        "sha1": record["sha1"],
        "is_first_in_box": first,
        "is_new": False,
    }


def _manifest(item: dict) -> dict:
    return {"boxes": {"box1": [item]}}


def test_classify_force_snapshots_before_live_overwrite(
    pipeline_cfg, write_page, monkeypatch
):
    page = write_page(
        "p1.jpg",
        page_type="hearing",
        classify_model="old-classifier",
        classified_at="2026-01-01T00:00:00Z",
    )
    original = page.read_text()
    item = _item(page)
    monkeypatch.setattr(
        "court_pipeline.classify_transcribe.load_manifest",
        lambda cfg: _manifest(item),
    )

    def overwrite(cfg, todo, stats, process_one):
        page.write_text('{"replacement": true}', encoding="utf-8")

    monkeypatch.setattr(
        "court_pipeline.classify_transcribe._run_live", overwrite
    )

    stats = run_classify(pipeline_cfg, force=True, use_batch=False)

    snapshot = Path(stats["force_snapshot"])
    saved = snapshot / "pages" / "box1" / "p1.jpg.json"
    assert saved.read_text() == original
    assert json.loads(page.read_text()) == {"replacement": True}


def test_transcribe_force_snapshots_before_skip_overwrite(
    pipeline_cfg, write_page, monkeypatch
):
    page = write_page(
        "blank.jpg",
        page_type="blank",
        verbatim_text="old text retained for audit",
        transcribe_model="old-transcriber",
    )
    original = page.read_text()
    item = _item(page)
    monkeypatch.setattr(
        "court_pipeline.classify_transcribe.load_manifest",
        lambda cfg: _manifest(item),
    )

    stats = run_transcribe(pipeline_cfg, force=True, use_batch=False)

    snapshot = Path(stats["force_snapshot"])
    saved = snapshot / "pages" / "box1" / "blank.jpg.json"
    assert saved.read_text() == original
    assert json.loads(page.read_text())["transcription_status"] == "skipped"


def test_cases_force_snapshots_matching_case_json(
    pipeline_cfg,
    write_page,
    make_case,
    write_cases_index,
    write_pipeline_case,
    monkeypatch,
):
    write_page("p1.jpg")
    write_cases_index([make_case("c1", ["p1.jpg"])])
    existing = write_pipeline_case(
        "c1", source_images=["p1.jpg"], plaintiff="old extraction"
    )
    original = existing.read_text()
    monkeypatch.setattr(
        "court_pipeline.consolidate.get_provider_named",
        lambda cfg, name: object(),
    )
    monkeypatch.setattr(
        "court_pipeline.consolidate._consolidate_one",
        lambda cfg, provider, case, issues: None,
    )

    stats = run_consolidate(pipeline_cfg, force=True)

    snapshot = Path(stats["force_snapshot"])
    saved = snapshot / "cases" / existing.name
    assert saved.read_text() == original
    manifest = json.loads((snapshot / "snapshot_manifest.json").read_text())
    assert manifest["reason"] == "cases_force"
    assert len(manifest["files"]) == 1
