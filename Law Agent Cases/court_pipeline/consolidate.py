"""Per-case consolidation: cover image + page transcripts -> structured CaseRecord JSON."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from .classify_transcribe import _as_object, _migrate_legacy
from .config import Config
from .inventory import resolve_image_path
from .pageio import prepare_image_bytes
from .prompts import CONSOLIDATION_PROMPT
from .providers import get_provider_named
from .providers.base import LLMRequest, Provider
from .schema import CaseRecord
from .util import now_iso, read_json, safe_slug, write_json


def _load_cases_index(cfg: Config) -> Dict[str, Any]:
    if not cfg.cases_index_path.exists():
        raise FileNotFoundError(
            f"cases index not found at {cfg.cases_index_path}; run the `segment` command first"
        )
    return read_json(cfg.cases_index_path)


def _read_page_cache(cache_file: str) -> Optional[Dict[str, Any]]:
    """Load a page cache record, applying the pre-two-pass migration.

    On-disk records from the original single-pass pipeline lack
    ``classified_at`` / ``transcription_status``. Classify/transcribe already
    migrate them on read; consolidation must do the same or it will treat a
    fully transcribed legacy page as ``pending`` and hold the case back.
    """
    p = Path(cache_file)
    if not p.exists():
        return None
    return _migrate_legacy(read_json(p))


def _page_verbatim_text(cache_file: str) -> str:
    """Read a page's cached ``verbatim_text`` (empty string if missing/unreadable)."""
    try:
        rec = _read_page_cache(cache_file)
    except Exception:
        return ""
    if rec is None:
        return ""
    return rec.get("verbatim_text", "") or ""


def _assemble_transcripts(cfg: Config, case: Dict[str, Any]) -> str:
    """Context block fed to the MODEL (every page, even skip types, for grounding)."""
    parts: List[str] = []
    for cache_file, fname, ptype in zip(
        case["page_cache_files"], case["page_files"], case["page_types"]
    ):
        text = _page_verbatim_text(cache_file)
        parts.append(f"[page: {fname} | type: {ptype}]\n{text}")
    return "\n\n".join(parts)


def page_issues(cfg: Config, case: Dict[str, Any]) -> List[str]:
    """Report pages that would be silently dropped from ``full_transcript``.

    ``assemble_full_transcript`` omits any page with empty ``verbatim_text``, so a
    page that failed transcription (rate limit, auth, parse error) or was never
    transcribed at all disappears from the assembled case with no trace: the case
    still consolidates, still gets a ``.done_`` marker, and simply comes out
    short. This pre-flight check is what makes that visible.

    Skip-type pages (``blank`` / ``box_photo``) are expected to be empty and are
    not reported. Pre-two-pass (legacy) page caches are migrated before the
    status check so a completed single-pass record is not mistaken for pending.
    Returns one human-readable string per problem page.
    """
    skip_types = set(cfg.transcribe_skip_types)
    issues: List[str] = []
    for cache_file, fname, ptype in zip(
        case["page_cache_files"], case["page_files"], case["page_types"]
    ):
        if ptype in skip_types:
            continue
        p = Path(cache_file)
        if not p.exists():
            issues.append(f"{fname}: page cache missing ({cache_file})")
            continue
        try:
            rec = _read_page_cache(cache_file)
        except Exception as exc:
            issues.append(f"{fname}: page cache unreadable ({exc})")
            continue
        if rec is None:
            issues.append(f"{fname}: page cache missing ({cache_file})")
            continue
        err = rec.get("transcribe_error")
        if err:
            issues.append(f"{fname}: transcribe_error ({str(err)[:160]})")
            continue
        status = rec.get("transcription_status") or "pending"
        if status not in ("done", "skipped"):
            issues.append(f"{fname}: not transcribed (status={status})")
            continue
        if status == "done" and not (rec.get("verbatim_text") or "").strip():
            issues.append(f"{fname}: transcribed but verbatim_text is empty")
    return issues


def assemble_full_transcript(cfg: Config, case: Dict[str, Any]) -> str:
    """Deterministically build the case's full hearing transcript IN CODE.

    Concatenates each page's cached ``verbatim_text`` in page order. This is the
    source of ``CaseRecord.full_transcript`` -- it is NOT produced by the model,
    so the verbatim text never drifts and is byte-for-byte the per-page output.

    Assembly rule:
      * pages are taken in the order recorded in ``cases.json`` (already page
        order from segmentation);
      * pages whose ``page_type`` is a transcribe skip type (``blank`` /
        ``box_photo`` by config) are omitted (they carry no transcription);
      * pages with empty/whitespace-only ``verbatim_text`` are omitted;
      * each remaining page is prefixed with a one-line header
        ``[<filename> | <page_type>]`` and pages are separated by a blank line.

    Silent omission is only safe because :func:`page_issues` gates consolidation:
    by the time a case reaches here, every non-skip page either has text or the
    caller passed ``--allow-incomplete`` and the gaps are recorded on the record.
    """
    skip_types = set(cfg.transcribe_skip_types)
    parts: List[str] = []
    for cache_file, fname, ptype in zip(
        case["page_cache_files"], case["page_files"], case["page_types"]
    ):
        if ptype in skip_types:
            continue
        text = _page_verbatim_text(cache_file)
        if not text.strip():
            continue
        parts.append(f"[{fname} | {ptype}]\n{text}")
    return "\n\n".join(parts)


def _case_out_path(cfg: Config, case: Dict[str, Any], case_number: Optional[str]) -> Path:
    num = safe_slug(case_number or case.get("provisional_case_number") or "no_number", 30)
    appeal = "_appeal" if case.get("is_appeal") else ""
    fname = f"{safe_slug(case['box'], 40)}__case_{num}{appeal}__{case['case_id'].split('__')[-1]}.json"
    return cfg.cases_out_dir / fname


def _is_done(cfg: Config, case: Dict[str, Any]) -> bool:
    # Stable marker file keyed by case_id (case_number may change between runs).
    marker = cfg.cases_out_dir / f".done_{safe_slug(case['case_id'], 80)}"
    return marker.exists()


def _mark_done(cfg: Config, case: Dict[str, Any]) -> None:
    marker = cfg.cases_out_dir / f".done_{safe_slug(case['case_id'], 80)}"
    marker.write_text(now_iso(), encoding="utf-8")


def _consolidate_one(
    cfg: Config,
    provider: Provider,
    case: Dict[str, Any],
    issues: Optional[List[str]] = None,
) -> Optional[str]:
    incomplete = list(issues or [])
    max_attempts = int(cfg.get("run", "max_retries", default=5))
    init = float(cfg.get("run", "retry_initial_seconds", default=2))
    mx = float(cfg.get("run", "retry_max_seconds", default=60))

    transcripts = _assemble_transcripts(cfg, case)
    prompt = CONSOLIDATION_PROMPT.format(transcripts=transcripts)
    # full_transcript is assembled DETERMINISTICALLY in code (never from the
    # model), so it stays byte-identical to the per-page verbatim output.
    full_transcript = assemble_full_transcript(cfg, case)

    images: List[bytes] = []
    cover_fn = case.get("cover_filename")
    box = case.get("box")
    if cover_fn and box:
        cover = resolve_image_path(cfg, box, cover_fn, case.get("cover_path"))
        if cover.is_file():
            try:
                images.append(prepare_image_bytes(cover, cfg))
            except Exception:
                images = []

    @retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=init, max=mx),
        reraise=True,
    )
    def _call() -> Dict[str, Any]:
        req = LLMRequest(
            # Output is now just the short structured fields (no full transcript),
            # so a modest budget is plenty.
            prompt=prompt, images=images, max_output_tokens=4096,
            key=case["case_id"], model=cfg.consolidate_model,
        )
        result = provider.generate(req)
        if result.error or result.parsed is None:
            raise RuntimeError(result.error or "no JSON parsed")
        return result.parsed

    try:
        parsed = _call()
    except Exception as exc:
        # Even when field extraction fails, the code-assembled verbatim
        # transcript is still valid and worth persisting.
        rec = CaseRecord(
            case_id=case["case_id"], box=case["box"], is_appeal=case.get("is_appeal", False),
            source_images=case["page_files"], page_range=case.get("page_range", []),
            provider=cfg.consolidate_provider, model=cfg.consolidate_model,
            processed_at=now_iso(), full_transcript=full_transcript, error=str(exc),
            incomplete_pages=incomplete,
        )
        write_json(_case_out_path(cfg, case, None), rec.model_dump())
        return str(exc)

    obj = _as_object(parsed)
    if obj is None:
        err = "response was not a JSON object"
        rec = CaseRecord(
            case_id=case["case_id"], box=case["box"], is_appeal=case.get("is_appeal", False),
            source_images=case["page_files"], page_range=case.get("page_range", []),
            provider=cfg.consolidate_provider, model=cfg.consolidate_model,
            processed_at=now_iso(), full_transcript=full_transcript, error=err,
            incomplete_pages=incomplete,
        )
        write_json(_case_out_path(cfg, case, None), rec.model_dump())
        return err

    # The model no longer returns full_transcript; ignore it if present and rate
    # the derived transcript as "derived" rather than a model confidence level.
    obj.pop("full_transcript", None)
    field_confidence = {k: str(v) for k, v in (obj.get("field_confidence") or {}).items()}
    field_confidence["full_transcript"] = "derived"

    rec = CaseRecord(
        case_id=case["case_id"],
        box=case["box"],
        is_appeal=bool(obj.get("is_appeal", case.get("is_appeal", False))),
        source_images=case["page_files"],
        page_range=case.get("page_range", []),
        provider=cfg.consolidate_provider,
        model=cfg.consolidate_model,
        processed_at=now_iso(),
        case_number=obj.get("case_number") or case.get("provisional_case_number"),
        district=obj.get("district"),
        magistrate=obj.get("magistrate"),
        plaintiff=obj.get("plaintiff"),
        defendant=obj.get("defendant"),
        claim=obj.get("claim"),
        date_heard=obj.get("date_heard"),
        date_heard_iso=obj.get("date_heard_iso"),
        appearance_for_plaintiff=obj.get("appearance_for_plaintiff"),
        appearance_for_defendant=obj.get("appearance_for_defendant"),
        lawyer_or_agent_for_plaintiff=obj.get("lawyer_or_agent_for_plaintiff"),
        lawyer_or_agent_for_defendant=obj.get("lawyer_or_agent_for_defendant"),
        interpreter=obj.get("interpreter"),
        plea_verbatim=obj.get("plea_verbatim"),
        verdict=obj.get("verdict"),
        full_transcript=full_transcript,
        language_notes=obj.get("language_notes"),
        field_confidence=field_confidence,
        uncertain_fields=list(obj.get("uncertain_fields") or []),
        incomplete_pages=incomplete,
    )
    write_json(_case_out_path(cfg, case, rec.case_number), rec.model_dump())
    _mark_done(cfg, case)
    return None


def _write_incomplete_report(cfg: Config, blocked: List[Dict[str, Any]]) -> Path:
    path = cfg.output_dir / "incomplete_cases.json"
    write_json(
        path,
        {
            "generated_at": now_iso(),
            "n_cases": len(blocked),
            "hint": (
                "These cases have pages with no usable transcription. Re-run "
                "`transcribe` (optionally --force for the affected box) to fill "
                "them, or re-run `cases --allow-incomplete` to consolidate anyway "
                "and record the gaps on each case record."
            ),
            "cases": blocked,
        },
    )
    return path


def run_consolidate(
    cfg: Config,
    boxes: List[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    allow_incomplete: bool = False,
) -> Dict[str, Any]:
    index = _load_cases_index(cfg)
    cfg.ensure_dirs()
    provider = get_provider_named(cfg, cfg.consolidate_provider)

    cases = index["cases"]
    if boxes:
        cases = [c for c in cases if c["box"] in boxes]

    todo: List[tuple[Dict[str, Any], List[str]]] = []
    blocked: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {
        "total": len(cases),
        "skipped_done": 0,
        "skipped_incomplete": 0,
        "queued": 0,
        "errors": 0,
    }
    for c in cases:
        if not force and _is_done(cfg, c):
            stats["skipped_done"] += 1
            continue
        # Pre-flight: a case with untranscribed pages would consolidate into a
        # silently short transcript, so it is held back rather than marked done.
        issues = page_issues(cfg, c)
        if issues and not allow_incomplete:
            stats["skipped_incomplete"] += 1
            blocked.append({"case_id": c["case_id"], "box": c["box"], "issues": issues})
            continue
        todo.append((c, issues))
        if limit and len(todo) >= limit:
            break
    stats["queued"] = len(todo)

    if blocked:
        report = _write_incomplete_report(cfg, blocked)
        stats["incomplete_report"] = str(report)
        preview = blocked[:5]
        tqdm.write(
            f"WARNING: {len(blocked)} case(s) held back for missing page "
            f"transcriptions; see {report}"
        )
        for item in preview:
            tqdm.write(f"  {item['case_id']}: {item['issues'][0]}")
        if len(blocked) > len(preview):
            tqdm.write(f"  ... and {len(blocked) - len(preview)} more")

    if not todo:
        return stats

    concurrency = int(cfg.get("run", "concurrency", default=6))
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(_consolidate_one, cfg, provider, c, issues): c for c, issues in todo
        }
        for fut in tqdm(as_completed(futures), total=len(futures), desc="cases"):
            if fut.result():
                stats["errors"] += 1
    return stats
