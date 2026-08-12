#!/usr/bin/env python3
"""Live transcribe pages with transcribe_error using Claude Opus (not batch)."""

from __future__ import annotations

import json
import sys
from typing import List, Tuple

from .classify_transcribe import _load_record_obj, _run_live, _transcribe_one_live
from .config import load_config
from .inventory import load_manifest

OPUS_MODEL = "claude-opus-4-8"
OPUS_PROVIDER = "anthropic"


def _patch_opus_transcribe(cfg) -> None:
    cfg._data.setdefault("models", {})["anthropic"] = OPUS_MODEL
    t = cfg._data.setdefault("stages", {}).setdefault("transcribe", {})
    t["default_provider"] = OPUS_PROVIDER
    t["default_model"] = OPUS_MODEL
    t["type_models"] = {}
    t["type_providers"] = {}
    t["mode"] = "live"


def _collect_error_pages(cfg) -> List[Tuple[str, str]]:
    """All manifest pages whose cache record has transcribe_error."""
    manifest = load_manifest(cfg)
    errors: List[Tuple[str, str]] = []
    for box, items in manifest["boxes"].items():
        for item in items:
            rec = _load_record_obj(cfg, box, item["filename"])
            if rec is not None and rec.transcribe_error:
                errors.append((box, item["filename"]))
    return sorted(errors)


def main() -> int:
    cfg = load_config()
    _patch_opus_transcribe(cfg)
    cfg.ensure_dirs()

    manifest = load_manifest(cfg)
    box_items = {box: {it["filename"]: it for it in items} for box, items in manifest["boxes"].items()}

    targets = _collect_error_pages(cfg)
    todo = []
    skipped = []
    for box, filename in targets:
        rec = _load_record_obj(cfg, box, filename)
        if rec is None or not rec.transcribe_error:
            skipped.append({"box": box, "filename": filename, "reason": "no transcribe_error"})
            continue
        item = box_items.get(box, {}).get(filename)
        if item is None:
            skipped.append({"box": box, "filename": filename, "reason": "not in manifest"})
            continue
        todo.append((box, item, OPUS_PROVIDER, OPUS_MODEL))

    stats = {"queued": len(todo), "errors": 0, "skipped": skipped}
    print(
        f"[retry_opus] {len(targets)} page(s) with transcribe_error; "
        f"{len(todo)} queued for {OPUS_MODEL} (live)",
        flush=True,
    )
    if todo:
        _run_live(cfg, todo, stats, _transcribe_one_live)

    results = []
    for box, filename in targets:
        rec = _load_record_obj(cfg, box, filename)
        if rec is None:
            results.append({"box": box, "filename": filename, "status": "missing"})
        elif rec.transcribe_error:
            results.append(
                {
                    "box": box,
                    "filename": filename,
                    "status": "error",
                    "transcribe_error": rec.transcribe_error,
                    "transcribe_model": rec.transcribe_model,
                }
            )
        elif rec.transcription_status == "done":
            results.append(
                {
                    "box": box,
                    "filename": filename,
                    "status": "ok",
                    "transcribe_model": rec.transcribe_model,
                    "verbatim_len": len(rec.verbatim_text or ""),
                }
            )
        else:
            results.append({"box": box, "filename": filename, "status": rec.transcription_status})

    report = {
        "provider": OPUS_PROVIDER,
        "model": OPUS_MODEL,
        "mode": "live",
        "stats": stats,
        "results": results,
        "recovered": sum(1 for r in results if r.get("status") == "ok"),
        "remaining_errors": sum(1 for r in results if r.get("status") == "error"),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["remaining_errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
