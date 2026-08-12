#!/usr/bin/env python3
"""Create Claude transcript alternates for pages whose baseline failed.

The normal pipeline owns ``PageRecord.verbatim_text`` (the Gemini/default
baseline). This recovery script must never call the normal transcription writer:
doing so would replace that baseline and its provenance with Opus output. Claude
results instead live under ``transcription_alternates["claude"]`` and flow into
the viewer's tri-value ``claude`` slot on the next build_results run.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Tuple

from .classify_transcribe import (
    _as_object,
    _base_record,
    _cache_path,
    _generate_with_retries,
    _load_record_obj,
    _run_live,
    _transcribe_request,
)
from .config import load_config
from .inventory import load_manifest
from .prompts import TRANSCRIBE_PROMPT
from .provenance import stage_provenance
from .providers import get_provider_named
from .snapshots import snapshot_json_files
from .util import DailyQuotaExceeded, now_iso, write_json

OPUS_MODEL = "claude-opus-4-8"
OPUS_PROVIDER = "anthropic"
ALTERNATE_KEY = "claude"


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


def _transcribe_opus_alternate(
    cfg,
    box: str,
    item: dict,
    provider_name: str,
    model: str,
    stop_event=None,
) -> Optional[str]:
    """Write an Opus alternate without changing any baseline transcription field."""
    if stop_event is not None and stop_event.is_set():
        return "__quota__"

    out_path = _cache_path(cfg, box, item["filename"])
    provenance = stage_provenance(TRANSCRIBE_PROMPT)
    rec = _base_record(cfg, box, item)
    alternate = {
        "provider": provider_name,
        "model": model,
        "model_version": "",
        "transcribed_at": now_iso(),
        "prompt_hash": provenance["prompt_hash"],
        "git_commit": provenance["git_commit"],
        "git_dirty": provenance["git_dirty"],
        "verbatim_text": "",
        "error": None,
    }

    try:
        provider = get_provider_named(cfg, provider_name)
        req = _transcribe_request(cfg, box, item, model)
        result = _generate_with_retries(cfg, provider, req)
        obj = _as_object(result.parsed)
        if obj is None:
            raise RuntimeError("Opus response was not a JSON object")
        alternate["model_version"] = result.model_version or ""
        alternate["verbatim_text"] = str(obj.get("verbatim_text") or "")
        if not alternate["verbatim_text"].strip():
            raise RuntimeError("Opus response contained empty verbatim_text")
        alternate["languages"] = list(obj.get("languages") or [])
        alternate["notes"] = obj.get("notes")
    except DailyQuotaExceeded:
        if stop_event is not None:
            stop_event.set()
        return "__quota__"
    except Exception as exc:
        alternate["error"] = str(exc)
        rec.transcription_alternates[ALTERNATE_KEY] = alternate
        write_json(out_path, rec.model_dump())
        return str(exc)

    rec.transcription_alternates[ALTERNATE_KEY] = alternate
    write_json(out_path, rec.model_dump())
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create Claude alternates for pages whose baseline transcription failed."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="replace an existing successful Claude alternate",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
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
        alternate = (rec.transcription_alternates or {}).get(ALTERNATE_KEY) or {}
        if alternate.get("verbatim_text") and not alternate.get("error") and not args.force:
            skipped.append({
                "box": box,
                "filename": filename,
                "reason": "Claude alternate already exists (use --force to replace)",
            })
            continue
        todo.append((box, item, OPUS_PROVIDER, OPUS_MODEL))

    stats = {"queued": len(todo), "errors": 0, "skipped": skipped}
    print(
        f"[retry_opus] {len(targets)} page(s) with transcribe_error; "
        f"{len(todo)} queued for {OPUS_MODEL} (live)",
        flush=True,
    )
    if args.force:
        snapshot = snapshot_json_files(
            cfg,
            (
                _cache_path(cfg, box, item["filename"])
                for box, item, _, _ in todo
            ),
            "retry_opus_force",
        )
        if snapshot is not None:
            stats["force_snapshot"] = str(snapshot)
    if todo:
        _run_live(cfg, todo, stats, _transcribe_opus_alternate)

    results = []
    for box, filename in targets:
        rec = _load_record_obj(cfg, box, filename)
        if rec is None:
            results.append({"box": box, "filename": filename, "status": "missing"})
        else:
            alternate = (rec.transcription_alternates or {}).get(ALTERNATE_KEY) or {}
            alt_error = alternate.get("error")
            alt_text = alternate.get("verbatim_text") or ""
            if alt_error:
                results.append(
                    {
                        "box": box,
                        "filename": filename,
                        "status": "error",
                        "alternate_error": alt_error,
                        "alternate_model": alternate.get("model"),
                        "baseline_error_preserved": rec.transcribe_error,
                    }
                )
            elif alt_text:
                results.append(
                    {
                        "box": box,
                        "filename": filename,
                        "status": "ok",
                        "alternate_model": alternate.get("model"),
                        "alternate_model_version": alternate.get("model_version"),
                        "verbatim_len": len(alt_text),
                        "baseline_error_preserved": rec.transcribe_error,
                    }
                )
            else:
                results.append(
                    {
                        "box": box,
                        "filename": filename,
                        "status": "missing_alternate",
                        "baseline_error_preserved": rec.transcribe_error,
                    }
                )

    report = {
        "provider": OPUS_PROVIDER,
        "model": OPUS_MODEL,
        "mode": "live",
        "destination": 'PageRecord.transcription_alternates["claude"]',
        "baseline_verbatim_text_overwritten": False,
        "stats": stats,
        "results": results,
        "recovered": sum(1 for r in results if r.get("status") == "ok"),
        "remaining_errors": sum(
            1 for r in results if r.get("status") in ("error", "missing_alternate", "missing")
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["remaining_errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
