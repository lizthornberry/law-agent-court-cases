"""Build/refresh the canonical ``results.json`` from pipeline outputs.

Reads (READ-ONLY) the pipeline's per-case JSONs (``output/cases/*.json``) and
per-page JSONs (``data/pages/<box>/<filename>.json``) and produces the
court_viewer ``results.json`` described in :mod:`court_viewer.viewer_schema`.

Idempotent + edit-preserving: when an existing ``results.json`` is present, it is
merged by ``case_id`` so that user-supplied data survives a regeneration:

* PRESERVED from the old file: ``edited`` slots (fields + page transcripts),
  ``claude`` alternates, user ``notes.edited``, ``review_status``.
* REFRESHED from the pipeline: ``gemini`` slots (fields + page transcripts),
  ``page_type``, ``order``, ``page_range``, ``source_images``, ``is_appeal``,
  ``field_confidence``, ``provenance``.

Usage::

    python -m court_viewer.build_results [--config path] [--output path]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config, load_config
from .viewer_schema import (
    FIELD_NAMES,
    SCHEMA_VERSION,
    empty_trivalue,
    normalize_case,
    normalize_notes,
)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _load_pipeline_cases(cases_dir: Path) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    if not cases_dir.is_dir():
        return cases
    for path in sorted(cases_dir.glob("*.json")):
        data = _read_json(path)
        if isinstance(data, dict) and data.get("case_id"):
            cases.append(data)
    return cases


def _load_page_record(pages_dir: Path, box: str, filename: str) -> Optional[Dict[str, Any]]:
    """Load a per-page record. Pages live at pages_dir/<box>/<filename>.json."""
    candidate = pages_dir / box / (filename + ".json")
    if candidate.is_file():
        return _read_json(candidate)
    return None


def _build_pages(case: Dict[str, Any], pages_dir: Path) -> List[Dict[str, Any]]:
    box = case.get("box", "")
    pages: List[Dict[str, Any]] = []
    for idx, filename in enumerate(case.get("source_images") or []):
        rec = _load_page_record(pages_dir, box, filename) or {}
        gemini_text = rec.get("verbatim_text") or None
        tri = empty_trivalue()
        tri["gemini"] = gemini_text
        pages.append(
            {
                "filename": filename,
                "order": int(rec.get("order", idx) or idx),
                "page_type": rec.get("page_type", "other") or "other",
                "transcript": tri,
            }
        )
    return pages


def _case_from_pipeline(case: Dict[str, Any], pages_dir: Path) -> Dict[str, Any]:
    """Convert a pipeline CaseRecord dict into a fresh results.json case."""
    fields: Dict[str, Any] = {}
    for name in FIELD_NAMES:
        tri = empty_trivalue()
        val = case.get(name)
        tri["gemini"] = val if (val is None or isinstance(val, str)) else str(val)
        fields[name] = tri

    return normalize_case(
        {
            "case_id": case.get("case_id", ""),
            "box": case.get("box", ""),
            "is_appeal": bool(case.get("is_appeal", False)),
            "page_range": list(case.get("page_range") or []),
            "review_status": None,  # default applied by normalize_case
            "notes": empty_trivalue(),
            "source_images": list(case.get("source_images") or []),
            "fields": fields,
            "pages": _build_pages(case, pages_dir),
            "field_confidence": dict(case.get("field_confidence") or {}),
            "provenance": {
                "provider": case.get("provider", ""),
                "model": case.get("model", ""),
                "processed_at": case.get("processed_at", ""),
            },
        }
    )


def _merge_case(fresh: Dict[str, Any], old: Dict[str, Any]) -> Dict[str, Any]:
    """Merge user-owned data from ``old`` into a ``fresh`` (pipeline) case."""
    old = normalize_case(old)

    # User-owned, case-level.
    fresh["review_status"] = old.get("review_status", fresh["review_status"])
    old_notes = normalize_notes(old.get("notes"))
    fresh["notes"] = empty_trivalue()
    fresh["notes"]["edited"] = old_notes.get("edited")

    # Fields: keep edited + claude from old, keep refreshed gemini from fresh.
    for name in FIELD_NAMES:
        old_tri = old["fields"].get(name) or empty_trivalue()
        fresh["fields"][name]["edited"] = old_tri.get("edited")
        fresh["fields"][name]["claude"] = old_tri.get("claude")

    # Pages: match by filename; keep edited + claude transcripts from old.
    old_pages_by_name = {p["filename"]: p for p in old.get("pages", [])}
    for page in fresh["pages"]:
        old_page = old_pages_by_name.get(page["filename"])
        if old_page:
            old_tri = old_page.get("transcript") or empty_trivalue()
            page["transcript"]["edited"] = old_tri.get("edited")
            page["transcript"]["claude"] = old_tri.get("claude")

    return fresh


def build_results(
    config: Config,
    output_path: Optional[Path] = None,
    existing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the results.json document (and optionally write it).

    If ``existing`` is provided (or an output file already exists), user edits are
    merged in. Returns the document dict.
    """
    out_path = output_path or config.results_json

    if existing is None and out_path.is_file():
        existing = _read_json(out_path)
    old_by_id: Dict[str, Dict[str, Any]] = {}
    if isinstance(existing, dict):
        for c in existing.get("cases", []):
            cid = c.get("case_id")
            if cid:
                old_by_id[cid] = c

    pipeline_cases = _load_pipeline_cases(config.pipeline_cases_dir)

    cases_out: List[Dict[str, Any]] = []
    seen_ids = set()
    for pc in pipeline_cases:
        fresh = _case_from_pipeline(pc, config.pipeline_pages_dir)
        cid = fresh["case_id"]
        if not cid or cid in seen_ids:
            continue
        seen_ids.add(cid)
        if cid in old_by_id:
            fresh = _merge_case(fresh, old_by_id[cid])
        cases_out.append(fresh)

    # Preserve any cases that exist only in results.json (e.g. synthesized sample
    # cases, or cases whose pipeline output was removed) so user work is not lost.
    for cid, old_case in old_by_id.items():
        if cid not in seen_ids:
            cases_out.append(normalize_case(old_case))

    cases_out.sort(key=lambda c: (c.get("box", ""), c.get("page_range") or [], c.get("case_id", "")))

    doc = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "archive_root": str(config.archive_root),
        "cases": cases_out,
    }

    if output_path is not None or out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)

    return doc


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build court_viewer results.json from pipeline outputs.")
    parser.add_argument("--config", default=None, help="Path to court_viewer config.yaml")
    parser.add_argument("--output", default=None, help="Override output results.json path")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    out_path = Path(args.output).resolve() if args.output else config.results_json
    doc = build_results(config, output_path=out_path)
    print(f"Wrote {len(doc['cases'])} cases -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
