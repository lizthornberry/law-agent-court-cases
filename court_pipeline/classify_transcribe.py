"""Two-pass per-image VLM processing.

The per-image work is split into two independently-runnable, independently-cached
and resumable passes that BOTH write into the SAME PageRecord JSON at
``data/pages/<box>/<filename>.json`` (so ``segment.py`` and ``consolidate.py``
keep working unchanged):

  Pass A "classify"  (run_classify):  cheap/fast model reads page_type +
      margin_case_number + rotation + languages (NO transcription).
  Pass B "transcribe" (run_transcribe): for each classified page, route to a
      model by page_type (config ``stages.transcribe``). Skip types
      (blank/box_photo) get verbatim_text="" with no API call.

Each pass is keyed on the image content sha1 so runs are resumable and
incremental. ``run_pages`` runs classify then transcribe for convenience.

Legacy single-pass caches (produced before this refactor) are migrated on read
so they are treated as already classified AND transcribed -- re-running the new
passes reports them as skipped rather than re-calling the (expensive) model.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential
from tqdm import tqdm

from .config import Config
from .inventory import iter_images, load_manifest
from .pageio import prepare_image_bytes
from .prompts import CLASSIFY_PROMPT, TRANSCRIBE_PROMPT
from .providers import get_provider_named
from .providers.base import LLMRequest, LLMResult, Provider
from .schema import PAGE_TYPES, PageRecord
from .util import DailyQuotaExceeded, is_daily_quota_error, now_iso, read_json, write_json

# Default skip types used only for legacy-cache migration (config is the source
# of truth at runtime).
_LEGACY_SKIP_TYPES = {"blank", "box_photo"}


def _cache_path(cfg: Config, box: str, filename: str) -> Path:
    return cfg.pages_dir / box / f"{filename}.json"


# -- record loading + legacy migration --------------------------------------
def _migrate_legacy(d: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade a pre-two-pass record in place so it reads as already done.

    Old records only had {page_type, verbatim_text, ...} with no per-pass state.
    Treat them as classified+transcribed using their existing model/timestamp so
    the new passes don't needlessly re-call the model on the calibration corpus.
    """
    # New-shape records (written by the two-pass code) ALWAYS carry the per-pass
    # keys, even when their values are empty (e.g. an error record has
    # classified_at="" but classify_error set). Only records missing these keys
    # are genuinely pre-refactor legacy. Checking key *presence* (not truthiness)
    # avoids mis-migrating an error record -- which would otherwise wipe its
    # classify_error and make a failed page look done.
    if "classified_at" in d or "classify_model" in d or "transcription_status" in d:
        return d  # already new-shape
    processed = d.get("processed_at") or ""
    model = d.get("model") or ""
    err = d.get("error")
    if processed or d.get("page_type"):
        d["classified_at"] = processed
        d["classify_model"] = model
        d["classify_error"] = err
        d["transcribed_at"] = processed
        d["transcribe_model"] = model
        d["transcribe_error"] = err
        page_type = d.get("page_type") or "other"
        if d.get("transcription_status") in (None, "", "pending"):
            d["transcription_status"] = (
                "skipped" if page_type in _LEGACY_SKIP_TYPES else "done"
            )
    return d


def _load_record_obj(cfg: Config, box: str, filename: str) -> Optional[PageRecord]:
    path = _cache_path(cfg, box, filename)
    if not path.exists():
        return None
    try:
        return PageRecord(**_migrate_legacy(read_json(path)))
    except Exception:
        return None


def _existing_for_item(cfg: Config, box: str, item: Dict[str, Any]) -> Optional[PageRecord]:
    """Return the cached record only if it matches the current content hash."""
    rec = _load_record_obj(cfg, box, item["filename"])
    if rec is None or rec.sha1 != item.get("sha1"):
        return None
    return rec


def _base_record(cfg: Config, box: str, item: Dict[str, Any]) -> PageRecord:
    """Existing (sha1-matching) record to update, else a fresh one."""
    existing = _existing_for_item(cfg, box, item)
    if existing is not None:
        return existing
    return PageRecord(
        box=box,
        filename=item["filename"],
        path=item["path"],
        order=item["order"],
        sha1=item["sha1"],
        provider=cfg.provider,
    )


# -- done checks (keyed on sha1) --------------------------------------------
def _classify_done(cfg: Config, box: str, item: Dict[str, Any]) -> bool:
    rec = _existing_for_item(cfg, box, item)
    if rec is None or rec.classify_error:
        return False
    return bool(rec.classified_at)


def _transcribe_done(cfg: Config, box: str, item: Dict[str, Any]) -> bool:
    rec = _existing_for_item(cfg, box, item)
    if rec is None or rec.transcribe_error:
        return False
    return rec.transcription_status in ("done", "skipped")


# -- response coercion ------------------------------------------------------
def _as_object(parsed: Any) -> Optional[Dict[str, Any]]:
    """Coerce a parsed JSON value to a dict.

    Vision models occasionally wrap the expected object in a one-element JSON
    array; tolerate that. Returns None when no object can be recovered so the
    caller records a per-image error (and the page is retried later) instead of
    crashing the whole run.
    """
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        for x in parsed:
            if isinstance(x, dict):
                return x
    return None


# -- shared retry wrapper ---------------------------------------------------
def _generate_with_retries(cfg: Config, provider: Provider, req: LLMRequest) -> Dict[str, Any]:
    """Call provider.generate with retries. Raises DailyQuotaExceeded or Exception."""
    max_attempts = int(cfg.get("run", "max_retries", default=5))
    init = float(cfg.get("run", "retry_initial_seconds", default=2))
    mx = float(cfg.get("run", "retry_max_seconds", default=60))

    @retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=init, max=mx),
        retry=retry_if_not_exception_type(DailyQuotaExceeded),
        reraise=True,
    )
    def _call() -> Dict[str, Any]:
        result = provider.generate(req)
        if result.error and is_daily_quota_error(result.error):
            raise DailyQuotaExceeded(result.error)
        if result.error or result.parsed is None:
            raise RuntimeError(result.error or "no JSON parsed")
        obj = _as_object(result.parsed)
        if obj is None:
            # A non-object response is usually a truncated/garbled generation
            # (e.g. a thinking model overrunning max_output_tokens). Treat it as
            # retryable so the backoff can recover it.
            raise RuntimeError("response was not a JSON object")
        return obj

    return _call()


# ===========================================================================
# Pass A: classify
# ===========================================================================
def _classify_request(cfg: Config, item: Dict[str, Any]) -> LLMRequest:
    img_bytes = prepare_image_bytes(Path(item["path"]), cfg)
    return LLMRequest(
        prompt=CLASSIFY_PROMPT,
        images=[img_bytes],
        # Generous budget: gemini-2.5-flash is a "thinking" model whose reasoning
        # tokens also count against this limit; too small a value can truncate
        # the JSON object (it then parses as a stray inner array).
        max_output_tokens=8192,
        key=item["path"],
        model=cfg.classify_model,
    )


def _apply_classify(
    rec: PageRecord, cfg: Config, parsed: Dict[str, Any], provider_name: str
) -> PageRecord:
    page_type = str(parsed.get("page_type") or "other")
    if page_type not in PAGE_TYPES:
        page_type = "other"
    rec.page_type = page_type
    rec.margin_case_number = parsed.get("margin_case_number") or None
    rec.detected_rotation_degrees = int(parsed.get("detected_rotation_degrees") or 0)
    rec.languages = list(parsed.get("languages") or [])
    if parsed.get("notes"):
        rec.notes = parsed.get("notes")
    rec.provider = provider_name
    rec.classified_at = now_iso()
    rec.classify_model = cfg.classify_model
    rec.classify_error = None
    rec.error = None
    rec.processed_at = rec.classified_at
    rec.model = cfg.classify_model
    return rec


def _box_photo_classify(cfg: Config, box: str, item: Dict[str, Any]) -> PageRecord:
    rec = _base_record(cfg, box, item)
    rec.page_type = "box_photo"
    rec.margin_case_number = None
    rec.detected_rotation_degrees = 0
    rec.languages = []
    rec.notes = "auto-marked box_photo (first image in box; skip_first_image_in_box)"
    rec.provider = cfg.provider
    rec.classified_at = now_iso()
    rec.classify_model = "(auto)"
    rec.classify_error = None
    rec.error = None
    rec.processed_at = rec.classified_at
    rec.model = "(auto)"
    return rec


def _classify_one_live(
    cfg: Config, box: str, item: Dict[str, Any], provider_name: str,
    stop_event: Optional[threading.Event] = None,
) -> Optional[str]:
    if stop_event is not None and stop_event.is_set():
        return "__quota__"
    out_path = _cache_path(cfg, box, item["filename"])
    try:
        provider = get_provider_named(cfg, provider_name)
        req = _classify_request(cfg, item)
        parsed = _generate_with_retries(cfg, provider, req)
        obj = _as_object(parsed)
        if obj is None:
            raise RuntimeError("classify response was not a JSON object")
    except DailyQuotaExceeded:
        if stop_event is not None:
            stop_event.set()
        return "__quota__"
    except Exception as exc:
        rec = _base_record(cfg, box, item)
        rec.classify_error = str(exc)
        rec.classify_model = cfg.classify_model
        rec.provider = provider_name
        rec.processed_at = now_iso()
        write_json(out_path, rec.model_dump())
        return str(exc)
    rec = _apply_classify(_base_record(cfg, box, item), cfg, obj, provider_name)
    write_json(out_path, rec.model_dump())
    return None


def run_classify(
    cfg: Config,
    boxes: List[str] | None = None,
    limit: int | None = None,
    new_only: bool = False,
    force: bool = False,
    use_batch: bool | None = None,
) -> Dict[str, int]:
    """Pass A: classify every (non-first) image. Resumable, keyed on sha1."""
    manifest = load_manifest(cfg)
    cfg.ensure_dirs()
    provider_name = cfg.classify_provider

    use_batch = cfg.stage_use_batch("classify", use_batch)
    skip_first = bool(cfg.get("run", "skip_first_image_in_box", default=True))

    todo: List[tuple] = []  # (box, item, provider_name)
    stats = {"total": 0, "skipped_done": 0, "box_photo": 0, "queued": 0, "errors": 0}

    for box, item in iter_images(manifest, boxes):
        stats["total"] += 1
        if new_only and not item.get("is_new", False):
            continue
        if skip_first and item.get("is_first_in_box"):
            if force or not _classify_done(cfg, box, item):
                rec = _box_photo_classify(cfg, box, item)
                write_json(_cache_path(cfg, box, item["filename"]), rec.model_dump())
            stats["box_photo"] += 1
            continue
        if not force and _classify_done(cfg, box, item):
            stats["skipped_done"] += 1
            continue
        todo.append((box, item, provider_name))
        if limit and len(todo) >= limit:
            break

    stats["queued"] = len(todo)
    if not todo:
        return stats

    if use_batch:
        if not get_provider_named(cfg, provider_name).supports_batch:
            raise RuntimeError(
                f"Batch mode requested but provider {provider_name!r} does not support batch. "
                "Use run.mode: live or a Gemini provider."
            )
        _run_batch(cfg, todo, stats, "classify")
    else:
        _run_live(cfg, todo, stats, _classify_one_live)
    return stats


# ===========================================================================
# Pass B: transcribe
# ===========================================================================
def _transcribe_max_output_tokens(cfg: Config) -> int:
    env = os.environ.get("COURT_PIPELINE_TRANSCRIBE_MAX_OUTPUT_TOKENS")
    if env:
        return int(env)
    return int(cfg.get("run", "transcribe_max_output_tokens", default=8192))


def _transcribe_request(cfg: Config, item: Dict[str, Any], model: str) -> LLMRequest:
    img_bytes = prepare_image_bytes(Path(item["path"]), cfg)
    return LLMRequest(
        prompt=TRANSCRIBE_PROMPT,
        images=[img_bytes],
        max_output_tokens=_transcribe_max_output_tokens(cfg),
        key=item["path"],
        model=model,
    )


def _apply_transcribe(
    rec: PageRecord, parsed: Dict[str, Any], model: str, provider_name: str
) -> PageRecord:
    rec.verbatim_text = str(parsed.get("verbatim_text") or "")
    if parsed.get("languages"):
        rec.languages = list(parsed.get("languages") or [])
    if parsed.get("notes"):
        rec.notes = parsed.get("notes")
    rec.provider = provider_name
    rec.transcribed_at = now_iso()
    rec.transcribe_model = model
    rec.transcribe_error = None
    rec.transcription_status = "done"
    rec.processed_at = rec.transcribed_at
    rec.model = model
    return rec


def _write_skip(cfg: Config, box: str, item: Dict[str, Any]) -> None:
    rec = _base_record(cfg, box, item)
    rec.verbatim_text = ""
    rec.transcribed_at = now_iso()
    rec.transcribe_model = "(skipped)"
    rec.transcribe_error = None
    rec.transcription_status = "skipped"
    write_json(_cache_path(cfg, box, item["filename"]), rec.model_dump())


def _transcribe_one_live(
    cfg: Config, box: str, item: Dict[str, Any], provider_name: str, model: str,
    stop_event: Optional[threading.Event] = None,
) -> Optional[str]:
    if stop_event is not None and stop_event.is_set():
        return "__quota__"
    out_path = _cache_path(cfg, box, item["filename"])
    try:
        provider = get_provider_named(cfg, provider_name)
        req = _transcribe_request(cfg, item, model)
        parsed = _generate_with_retries(cfg, provider, req)
        obj = _as_object(parsed)
        if obj is None:
            raise RuntimeError("transcribe response was not a JSON object")
    except DailyQuotaExceeded:
        if stop_event is not None:
            stop_event.set()
        return "__quota__"
    except Exception as exc:
        rec = _base_record(cfg, box, item)
        rec.transcribe_error = str(exc)
        rec.transcribe_model = model
        rec.provider = provider_name
        rec.processed_at = now_iso()
        write_json(out_path, rec.model_dump())
        return str(exc)
    rec = _apply_transcribe(_base_record(cfg, box, item), obj, model, provider_name)
    write_json(out_path, rec.model_dump())
    return None


def run_transcribe(
    cfg: Config,
    boxes: List[str] | None = None,
    limit: int | None = None,
    new_only: bool = False,
    force: bool = False,
    use_batch: bool | None = None,
) -> Dict[str, int]:
    """Pass B: transcribe each classified page via its routed model.

    Requires Pass A to have run; pages that aren't classified yet are reported
    under ``not_classified`` and left for a later run.
    """
    manifest = load_manifest(cfg)
    cfg.ensure_dirs()

    use_batch = cfg.stage_use_batch("transcribe", use_batch)
    skip_types = set(cfg.transcribe_skip_types)

    todo: List[tuple] = []  # (box, item, provider_name, model)
    stats = {
        "total": 0, "skipped_done": 0, "skipped_type": 0, "not_classified": 0,
        "queued": 0, "errors": 0,
    }

    for box, item in iter_images(manifest, boxes):
        stats["total"] += 1
        if new_only and not item.get("is_new", False):
            continue
        rec = _existing_for_item(cfg, box, item)
        if rec is None or not rec.classified_at or rec.classify_error:
            stats["not_classified"] += 1
            continue
        if rec.page_type in skip_types:
            if force or not _transcribe_done(cfg, box, item):
                _write_skip(cfg, box, item)
            stats["skipped_type"] += 1
            continue
        if not force and _transcribe_done(cfg, box, item):
            stats["skipped_done"] += 1
            continue
        model = cfg.transcribe_model_for(rec.page_type)
        provider_name = cfg.transcribe_provider_for(rec.page_type)
        todo.append((box, item, provider_name, model))
        if limit and len(todo) >= limit:
            break

    stats["queued"] = len(todo)
    if not todo:
        return stats

    if use_batch:
        print(
            f"[transcribe] {stats['queued']} page(s) queued for batch "
            f"(skipped_done={stats['skipped_done']}, skipped_type={stats['skipped_type']})",
            flush=True,
        )

    # Batch APIs are per-provider; every routed provider must support batch.
    if use_batch:
        unsupported = {
            entry[2] for entry in todo
            if not get_provider_named(cfg, entry[2]).supports_batch
        }
        if unsupported:
            raise RuntimeError(
                "Batch mode requested but routed provider(s) "
                f"{sorted(unsupported)!r} do not support batch. "
                "Use run.mode: live or Gemini providers."
            )
        _run_batch(cfg, todo, stats, "transcribe")
    else:
        _run_live(cfg, todo, stats, _transcribe_one_live)
    return stats


# ===========================================================================
# convenience: run both passes
# ===========================================================================
def run_pages(
    cfg: Config,
    boxes: List[str] | None = None,
    limit: int | None = None,
    new_only: bool = False,
    force: bool = False,
    use_batch: bool | None = None,
) -> Dict[str, Any]:
    """Run classify then transcribe (the old single `pages` command, now 2 passes)."""
    classify_stats = run_classify(
        cfg, boxes=boxes, limit=limit, new_only=new_only, force=force, use_batch=use_batch
    )
    transcribe_stats = run_transcribe(
        cfg, boxes=boxes, limit=limit, new_only=new_only, force=force, use_batch=use_batch
    )
    return {"classify": classify_stats, "transcribe": transcribe_stats}


# ===========================================================================
# executors (shared by both passes)
# ===========================================================================
def _run_live(
    cfg: Config, todo: List[tuple], stats: Dict[str, int],
    process_one: Callable[..., Optional[str]],
) -> None:
    """Concurrent live execution with a graceful daily-quota stop.

    `todo` items are tuples whose trailing element(s) after (box, item) are
    passed straight through to `process_one` (e.g. the routed provider + model).
    Each `process_one` resolves its own (memoized) provider, so a single run can
    mix providers per page type.
    """
    concurrency = int(cfg.get("run", "concurrency", default=6))
    stop_event = threading.Event()
    stats.setdefault("quota_skipped", 0)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        for entry in todo:
            box, item = entry[0], entry[1]
            extra = entry[2:]
            fut = pool.submit(process_one, cfg, box, item, *extra, stop_event)
            futures[fut] = entry
        for fut in tqdm(as_completed(futures), total=len(futures), desc="pages"):
            err = fut.result()
            if err == "__quota__":
                stats["quota_skipped"] += 1
            elif err:
                stats["errors"] += 1
    if stop_event.is_set():
        stats["quota_exhausted"] = True
        print(
            "\n[stop] Daily request quota exhausted. Processed work is cached; "
            "re-run the same command after the quota resets to resume."
        )


def _grouped_generate_batch(
    provider: Provider, requests: List[LLMRequest], pass_name: str = "transcribe",
) -> List[LLMResult]:
    """Run a batch, grouped by model (batch APIs are per-model)."""
    groups: "OrderedDict[Optional[str], List[LLMRequest]]" = OrderedDict()
    for r in requests:
        groups.setdefault(r.model, []).append(r)
    out: List[LLMResult] = []
    for model, reqs in groups.items():
        out.extend(provider.generate_batch(reqs, model=model, pass_name=pass_name))
    return out


def _write_batch_results(
    cfg: Config,
    results: List[LLMResult],
    index: Dict[str, tuple],
    stats: Dict[str, int],
    pass_name: str,
) -> None:
    for res in results:
        entry = index.get(res.key)
        if entry is None:
            continue
        box, item = entry[0], entry[1]
        provider_name = entry[2]
        out_path = _cache_path(cfg, box, item["filename"])
        obj = None if (res.error or res.parsed is None) else _as_object(res.parsed)
        if obj is None:
            rec = _base_record(cfg, box, item)
            err = res.error or "response was not a JSON object"
            if pass_name == "classify":
                rec.classify_error = err
                rec.classify_model = cfg.classify_model
            else:
                rec.transcribe_error = err
                rec.transcribe_model = entry[3]
            rec.provider = provider_name
            rec.processed_at = now_iso()
            write_json(out_path, rec.model_dump())
            stats["errors"] += 1
            continue
        if pass_name == "classify":
            rec = _apply_classify(_base_record(cfg, box, item), cfg, obj, provider_name)
        else:
            rec = _apply_transcribe(
                _base_record(cfg, box, item), obj, entry[3], provider_name
            )
        write_json(out_path, rec.model_dump())


def _run_batch(
    cfg: Config, todo: List[tuple], stats: Dict[str, int], pass_name: str,
) -> None:
    """File-based async batch for a pass. Batch failures are fatal (no live fallback).

    Entries carry a provider name (entry[2]); batch APIs are per-provider so we
    group entries by provider and submit one (model-grouped) batch per provider.
    For transcribe the routed model is entry[3].
    """
    print(f"[batch] preparing {len(todo)} {pass_name} requests ...", flush=True)
    index: Dict[str, tuple] = {}
    for entry in todo:
        index[entry[1]["path"]] = entry

    results: List[LLMResult] = []
    still_running: set[str] = set()
    by_provider: "OrderedDict[str, List[tuple]]" = OrderedDict()
    for entry in todo:
        by_provider.setdefault(entry[2], []).append(entry)

    for provider_name, entries in by_provider.items():
        provider = get_provider_named(cfg, provider_name)
        provider_still: set[str] = set()
        if hasattr(provider, "resume_pending_batches"):
            resumed, provider_still = provider.resume_pending_batches(pass_name)
            still_running |= provider_still
            if resumed:
                print(f"[batch] resumed {len(resumed)} result(s) from pending jobs", flush=True)
                _write_batch_results(cfg, resumed, index, stats, pass_name)
            if still_running:
                print(
                    f"[batch] {len(still_running)} page(s) still in-flight; skipping re-submit",
                    flush=True,
                )
        requests: List[LLMRequest] = []
        n_entries = len(entries)
        for i, entry in enumerate(entries, 1):
            item = entry[1]
            if item["path"] in still_running:
                continue
            if pass_name == "classify":
                req = _classify_request(cfg, item)
            else:
                req = _transcribe_request(cfg, item, entry[3])
            req.key = item["path"]
            req.pass_name = pass_name
            requests.append(req)
            if i == 1 or i % 100 == 0 or i == n_entries:
                print(
                    f"[batch] built {i}/{n_entries} {pass_name} requests "
                    f"(provider={provider_name})",
                    flush=True,
                )
        if not requests:
            continue
        results.extend(_grouped_generate_batch(provider, requests, pass_name=pass_name))

    if results:
        for res in tqdm(results, desc=f"{pass_name}-write"):
            _write_batch_results(cfg, [res], index, stats, pass_name)


def load_page_records(cfg: Config, boxes: List[str] | None = None) -> List[PageRecord]:
    """Load all cached page records (sorted by box then order). Migrates legacy."""
    records: List[PageRecord] = []
    if not cfg.pages_dir.exists():
        return records
    for box_dir in sorted(cfg.pages_dir.iterdir(), key=lambda p: p.name):
        if not box_dir.is_dir():
            continue
        if boxes and box_dir.name not in boxes:
            continue
        for jf in box_dir.glob("*.json"):
            try:
                records.append(PageRecord(**_migrate_legacy(read_json(jf))))
            except Exception:
                continue
    records.sort(key=lambda r: (r.box, r.order))
    return records
