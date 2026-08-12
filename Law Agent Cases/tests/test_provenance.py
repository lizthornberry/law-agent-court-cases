"""Run provenance: prompt identity, source revision, and provider model version."""

from __future__ import annotations

import json
from types import SimpleNamespace

from court_pipeline.classify_transcribe import _apply_classify, _apply_transcribe
from court_pipeline.consolidate import run_consolidate
from court_pipeline.prompts import CLASSIFY_PROMPT, CONSOLIDATION_PROMPT, TRANSCRIBE_PROMPT
from court_pipeline.provenance import code_provenance, prompt_hash
from court_pipeline.providers.anthropic import AnthropicProvider
from court_pipeline.providers.base import LLMRequest
from court_pipeline.providers.gemini import GeminiProvider
from court_pipeline.providers.mock import MockProvider
from court_pipeline.providers.openai import OpenAIProvider
from court_pipeline.schema import PageRecord
from court_viewer.build_results import build_results


def test_prompt_hash_is_stable_sha256_and_content_sensitive():
    digest = prompt_hash("exact prompt")

    assert len(digest) == 64
    assert digest == prompt_hash("exact prompt")
    assert digest != prompt_hash("exact prompt ")


def test_code_provenance_contains_a_real_git_sha():
    provenance = code_provenance()

    assert len(provenance["git_commit"]) == 40
    int(provenance["git_commit"], 16)
    assert isinstance(provenance["git_dirty"], bool)


def test_classify_records_prompt_code_and_response_model(pipeline_cfg):
    rec = _apply_classify(
        PageRecord(),
        pipeline_cfg,
        {"page_type": "hearing", "detected_rotation_degrees": 0},
        "mock",
        model_version="mock-classifier-build-17",
    )

    assert rec.classify_model == "mock-1"
    assert rec.classify_model_version == "mock-classifier-build-17"
    assert rec.classify_prompt_hash == prompt_hash(CLASSIFY_PROMPT)
    assert len(rec.classify_git_commit) == 40
    assert isinstance(rec.classify_git_dirty, bool)


def test_transcribe_records_prompt_code_and_response_model():
    rec = _apply_transcribe(
        PageRecord(),
        {"verbatim_text": "text"},
        "requested-preview-alias",
        "gemini",
        model_version="provider-resolved-model-2026-08-12",
    )

    assert rec.transcribe_model == "requested-preview-alias"
    assert rec.transcribe_model_version == "provider-resolved-model-2026-08-12"
    assert rec.transcribe_prompt_hash == prompt_hash(TRANSCRIBE_PROMPT)
    assert len(rec.transcribe_git_commit) == 40
    assert isinstance(rec.transcribe_git_dirty, bool)


def test_mock_provider_reports_the_requested_model(pipeline_cfg):
    result = MockProvider(pipeline_cfg).generate(
        LLMRequest(prompt=TRANSCRIBE_PROMPT, model="mock-routed-model")
    )

    assert result.model_version == "mock-routed-model"


def test_gemini_live_captures_response_model_version():
    response = SimpleNamespace(text='{"ok": true}', model_version="gemini-build-123")
    provider = GeminiProvider.__new__(GeminiProvider)
    provider.model = "requested-alias"
    provider.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kwargs: response)
    )
    provider._contents = lambda req: []
    provider._config = lambda req: {}

    result = provider.generate(LLMRequest(prompt="p"))

    assert result.parsed == {"ok": True}
    assert result.model_version == "gemini-build-123"


def test_gemini_batch_captures_response_model_version():
    provider = GeminiProvider.__new__(GeminiProvider)
    raw = json.dumps({
        "key": "page-1",
        "response": {
            "modelVersion": "gemini-batch-build-456",
            "candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}],
        },
    })

    result = provider._parse_result_jsonl(raw)["page-1"]

    assert result.parsed == {"ok": True}
    assert result.model_version == "gemini-batch-build-456"


def test_anthropic_captures_response_model():
    msg = SimpleNamespace(
        model="claude-resolved-20260812",
        content=[SimpleNamespace(type="text", text='{"ok": true}')],
    )
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.model = "claude-alias"
    provider.client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kwargs: msg)
    )

    result = provider.generate(LLMRequest(prompt="p"))

    assert result.model_version == "claude-resolved-20260812"


def test_openai_captures_response_model():
    response = SimpleNamespace(
        model="gpt-resolved-2026-08-12",
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
    )
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.model = "gpt-alias"
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: response)
        )
    )

    result = provider.generate(LLMRequest(prompt="p"))

    assert result.model_version == "gpt-resolved-2026-08-12"


def test_consolidated_case_records_full_provenance(
    pipeline_cfg, write_page, make_case, write_cases_index
):
    write_page("p1.jpg", verbatim_text="testimony")
    write_cases_index([make_case("c1", ["p1.jpg"])])

    stats = run_consolidate(pipeline_cfg)

    assert stats["errors"] == 0
    record = json.loads(next(pipeline_cfg.cases_out_dir.glob("*.json")).read_text())
    assert record["model"] == "mock-flash"
    assert record["model_version"] == "mock-flash"
    assert record["prompt_hash"] == prompt_hash(CONSOLIDATION_PROMPT)
    assert len(record["git_commit"]) == 40
    assert isinstance(record["git_dirty"], bool)


def test_case_provenance_flows_into_results_json(
    viewer_cfg, write_pipeline_case, write_page
):
    write_page("p1.jpg")
    write_pipeline_case(
        "c1",
        source_images=["p1.jpg"],
        model_version="provider-build",
        prompt_hash="a" * 64,
        git_commit="b" * 40,
        git_dirty=True,
    )

    case = build_results(viewer_cfg)["cases"][0]

    assert case["provenance"]["model_version"] == "provider-build"
    assert case["provenance"]["prompt_hash"] == "a" * 64
    assert case["provenance"]["git_commit"] == "b" * 40
    assert case["provenance"]["git_dirty"] is True
