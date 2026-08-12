"""Claude recovery must never overwrite the baseline page transcription."""

from __future__ import annotations

import json

from court_pipeline.providers.base import LLMResult
from court_pipeline.retry_opus_pages import _transcribe_opus_alternate


def _item(cfg, filename):
    record = json.loads((cfg.pages_dir / "box1" / f"{filename}.json").read_text())
    return {
        "filename": filename,
        "path": record["path"],
        "order": record["order"],
        "sha1": record["sha1"],
    }


def test_success_writes_claude_alternate_without_changing_baseline(
    pipeline_cfg, write_page, monkeypatch
):
    path = write_page(
        "hard.jpg",
        verbatim_text="GEMINI BASELINE",
        transcription_status="done",
        transcribe_error="Gemini parse failure",
        transcribe_model="gemini-preview",
        transcribe_model_version="gemini-resolved-build",
        transcribed_at="2026-01-01T00:00:00Z",
        provider="gemini",
        model="gemini-preview",
    )
    before = json.loads(path.read_text())
    monkeypatch.setattr(
        "court_pipeline.retry_opus_pages.get_provider_named",
        lambda cfg, name: object(),
    )
    monkeypatch.setattr(
        "court_pipeline.retry_opus_pages._transcribe_request",
        lambda cfg, box, item, model: object(),
    )
    monkeypatch.setattr(
        "court_pipeline.retry_opus_pages._generate_with_retries",
        lambda cfg, provider, req: LLMResult(
            text="",
            parsed={
                "verbatim_text": "CLAUDE ALTERNATE",
                "languages": ["English"],
                "notes": "difficult hand",
            },
            model_version="claude-resolved-build",
        ),
    )

    error = _transcribe_opus_alternate(
        pipeline_cfg,
        "box1",
        _item(pipeline_cfg, "hard.jpg"),
        "anthropic",
        "claude-opus-test",
    )

    assert error is None
    after = json.loads(path.read_text())
    # Every baseline value remains exactly as it was.
    for key in (
        "verbatim_text",
        "transcription_status",
        "transcribe_error",
        "transcribe_model",
        "transcribe_model_version",
        "transcribed_at",
        "provider",
        "model",
    ):
        assert after[key] == before[key], key
    alternate = after["transcription_alternates"]["claude"]
    assert alternate["verbatim_text"] == "CLAUDE ALTERNATE"
    assert alternate["model"] == "claude-opus-test"
    assert alternate["model_version"] == "claude-resolved-build"
    assert len(alternate["prompt_hash"]) == 64
    assert len(alternate["git_commit"]) == 40
    assert alternate["error"] is None


def test_failed_opus_attempt_still_does_not_change_baseline(
    pipeline_cfg, write_page, monkeypatch
):
    path = write_page(
        "hard.jpg",
        verbatim_text="GEMINI BASELINE",
        transcribe_error="Gemini failure",
        transcribe_model="gemini-preview",
    )
    before = json.loads(path.read_text())
    monkeypatch.setattr(
        "court_pipeline.retry_opus_pages.get_provider_named",
        lambda cfg, name: object(),
    )
    monkeypatch.setattr(
        "court_pipeline.retry_opus_pages._transcribe_request",
        lambda cfg, box, item, model: object(),
    )

    def fail(*args, **kwargs):
        raise RuntimeError("Anthropic unavailable")

    monkeypatch.setattr(
        "court_pipeline.retry_opus_pages._generate_with_retries", fail
    )

    error = _transcribe_opus_alternate(
        pipeline_cfg,
        "box1",
        _item(pipeline_cfg, "hard.jpg"),
        "anthropic",
        "claude-opus-test",
    )

    assert "Anthropic unavailable" in error
    after = json.loads(path.read_text())
    assert after["verbatim_text"] == before["verbatim_text"]
    assert after["transcribe_error"] == before["transcribe_error"]
    assert after["transcribe_model"] == before["transcribe_model"]
    assert (
        after["transcription_alternates"]["claude"]["error"]
        == "Anthropic unavailable"
    )
