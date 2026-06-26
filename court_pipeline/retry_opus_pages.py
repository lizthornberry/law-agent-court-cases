#!/usr/bin/env python3
"""One-off: live transcribe specific pages with Claude Opus (not batch)."""

from __future__ import annotations

import json
import sys
from typing import List, Tuple

from .classify_transcribe import _load_record_obj, _run_live, _transcribe_one_live
from .config import load_config
from .inventory import load_manifest

# The 5 Gemini JSON-parse failures to retry with Opus.
TARGETS: List[Tuple[str, str]] = [
    ("1-NKE 2-1-1-9", "IMG_6307.jpg"),
    ("1-NKE 2-1-1-14", "IMG_6609.jpg"),
    ("1-NKE 2-1-1-16 (2)", "DSC02077.JPG"),
    ("1-NKE 2-1-1-29", "IMG_7194.jpg"),
    ("1-NKE 2-1-1-39", "IMG_7511.jpg"),
]

OPUS_MODEL = "claude-opus-4-8"


def _patch_opus_transcribe(cfg) -> None:
    cfg._data.setdefault("models", {})["anthropic"] = OPUS_MODEL
    t = cfg._data.setdefault("stages", {}).setdefault("transcribe", {})
    t["default_provider"] = "anthropic"
    t["default_model"] = OPUS_MODEL
    t["mode"] = "live"


def main() -> int:
    cfg = load_config()
    _patch_opus_transcribe(cfg)
    cfg.ensure_dirs()

    manifest = load_manifest(cfg)
    box_items = {box: {it["filename"]: it for it in items} for box, items in manifest["boxes"].items()}

    todo = []
    skipped = []
    for box, filename in TARGETS:
        rec = _load_record_obj(cfg, box, filename)
        if rec is None or not rec.transcribe_error:
            skipped.append({"box": box, "filename": filename, "reason": "no transcribe_error"})
            continue
        item = box_items.get(box, {}).get(filename)
        if item is None:
            skipped.append({"box": box, "filename": filename, "reason": "not in manifest"})
            continue
        provider = cfg.transcribe_provider_for(rec.page_type)
        model = cfg.transcribe_model_for(rec.page_type)
        todo.append((box, item, provider, model))

    stats = {"queued": len(todo), "errors": 0, "skipped": skipped}
    if todo:
        print(f"[retry_opus] {len(todo)} page(s) with {OPUS_MODEL} (live)", flush=True)
        _run_live(cfg, todo, stats, _transcribe_one_live)

    results = []
    for box, filename in TARGETS:
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
        "model": OPUS_MODEL,
        "stats": stats,
        "results": results,
        "recovered": sum(1 for r in results if r.get("status") == "ok"),
        "remaining_errors": sum(1 for r in results if r.get("status") == "error"),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["remaining_errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
