"""Shared fixtures.

Every test runs against a throwaway tree under ``tmp_path`` and a throwaway local
state root, so nothing here can touch the real archive, the real ``results.json``
or the real ``~/.court-viewer``. No test makes a network call: the pipeline is
driven through the ``mock`` provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from court_pipeline.config import Config as PipelineConfig
from court_viewer.config import Config as ViewerConfig

BOX = "box1"


@pytest.fixture(autouse=True)
def isolated_local_root(tmp_path, monkeypatch):
    """Point COURT_VIEWER_HOME at a temp dir for the whole session.

    Autouse and unconditional: without it a stray test could write into the real
    ~/.court-viewer, and a test that asserts "the DB is outside OneDrive" would
    pass for the wrong reason.
    """
    root = tmp_path / "local_state"
    monkeypatch.setenv("COURT_VIEWER_HOME", str(root))
    return root


# ---------------------------------------------------------------------------
# court_pipeline
# ---------------------------------------------------------------------------
PIPELINE_CONFIG: Dict[str, Any] = {
    "provider": "mock",
    "models": {"mock": "mock-1"},
    "stages": {
        "classify": {"model": "mock-1"},
        "transcribe": {
            "default_model": "mock-pro",
            "skip_types": ["blank", "box_photo"],
        },
        "cases": {"model": "mock-flash"},
    },
    "paths": {
        "images_root": "images",
        "data_dir": "data",
        "output_dir": "output",
    },
    "run": {"mode": "live", "concurrency": 2, "max_retries": 1,
            "retry_initial_seconds": 0, "retry_max_seconds": 0},
    "segment": {
        "cover_types": ["cover_regular", "cover_appeal"],
        "split_on_margin_case_number_change": True,
    },
}


@pytest.fixture
def pipeline_cfg(tmp_path) -> PipelineConfig:
    """An isolated court_pipeline Config rooted at tmp_path."""
    root = tmp_path / "pipeline"
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "config.yaml"
    data = json.loads(json.dumps(PIPELINE_CONFIG))  # deep copy
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    cfg = PipelineConfig(data, config_path)
    cfg.ensure_dirs()
    (cfg.images_root / BOX).mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def write_page(pipeline_cfg):
    """Write a page cache record; returns its path.

    Defaults describe a healthy, fully transcribed hearing page so each test only
    has to state the one attribute it cares about.
    """
    def _write(
        filename: str,
        *,
        box: str = BOX,
        order: int = 0,
        page_type: str = "hearing",
        verbatim_text: str = "page text",
        transcription_status: str = "done",
        transcribe_error: Optional[str] = None,
        margin_case_number: Optional[str] = None,
        **extra: Any,
    ) -> Path:
        record = {
            "box": box,
            "filename": filename,
            "path": str(pipeline_cfg.images_root / box / filename),
            "order": order,
            "sha1": f"sha-{filename}",
            "page_type": page_type,
            "margin_case_number": margin_case_number,
            "verbatim_text": verbatim_text,
            "transcription_status": transcription_status,
            "transcribe_error": transcribe_error,
            "classified_at": "2026-01-01T00:00:00Z",
        }
        record.update(extra)
        path = pipeline_cfg.pages_dir / box / f"{filename}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def make_case(pipeline_cfg):
    """Build a cases.json-style case dict with correctly derived cache paths."""
    def _make(
        case_id: str,
        page_files: List[str],
        *,
        box: str = BOX,
        page_types: Optional[List[str]] = None,
        cache_files: Optional[List[str]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        case = {
            "case_id": case_id,
            "box": box,
            "is_appeal": False,
            "page_files": list(page_files),
            "page_types": list(page_types or ["hearing"] * len(page_files)),
            "page_cache_files": list(cache_files) if cache_files is not None else [
                str(pipeline_cfg.pages_dir / box / f"{f}.json") for f in page_files
            ],
            "page_paths": [str(pipeline_cfg.images_root / box / f) for f in page_files],
            "page_range": [0, max(0, len(page_files) - 1)],
        }
        case.update(extra)
        return case

    return _make


@pytest.fixture
def write_cases_index(pipeline_cfg):
    def _write(cases: List[Dict[str, Any]]) -> Path:
        pipeline_cfg.cases_index_path.write_text(
            json.dumps({"generated_at": "2026-01-01T00:00:00Z", "cases": cases}),
            encoding="utf-8",
        )
        return pipeline_cfg.cases_index_path

    return _write


# ---------------------------------------------------------------------------
# court_viewer
# ---------------------------------------------------------------------------
@pytest.fixture
def viewer_cfg(tmp_path, pipeline_cfg) -> ViewerConfig:
    """A viewer Config wired to the same tmp pipeline tree."""
    base = tmp_path / "viewer"
    base.mkdir(parents=True, exist_ok=True)
    data = {
        "paths": {
            "results_json": str(pipeline_cfg.output_dir / "results.json"),
            "db": "viewer.db",
            "thumbnails": "thumbnails",
            "backups": "backups",
            "archive_root": str(pipeline_cfg.images_root),
            "pipeline_cases_dir": str(pipeline_cfg.cases_out_dir),
            "pipeline_pages_dir": str(pipeline_cfg.pages_dir),
        },
        "backup": {"keep": 5},
    }
    return ViewerConfig(data, base / "config.yaml")


@pytest.fixture
def write_pipeline_case(pipeline_cfg):
    """Write an output/cases/*.json record, the input to build_results."""
    def _write(case_id: str, *, source_images: List[str], box: str = BOX, **fields: Any) -> Path:
        record = {
            "case_id": case_id,
            "box": box,
            "is_appeal": False,
            "source_images": list(source_images),
            "page_range": [0, max(0, len(source_images) - 1)],
            "provider": "mock",
            "model": "mock-flash",
            "processed_at": "2026-01-01T00:00:00Z",
            "field_confidence": {},
        }
        record.update(fields)
        path = pipeline_cfg.cases_out_dir / f"{case_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    return _write
