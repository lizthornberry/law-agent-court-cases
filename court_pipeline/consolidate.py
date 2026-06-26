"""Per-case consolidation: cover image + page transcripts -> structured CaseRecord JSON."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from .classify_transcribe import _as_object
from .config import Config
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


def _page_verbatim_text(cache_file: str) -> str:
    """Read a page's cached ``verbatim_text`` (empty string if missing/unreadable)."""
    p = Path(cache_file)
    if not p.exists():
        return ""
    try:
        return read_json(p).get("verbatim_text", "") or ""
    except Exception:
        return ""


def _assemble_transcripts(cfg: Config, case: Dict[str, Any]) -> str:
    """Context block fed to the MODEL (every page, even skip types, for grounding)."""
    parts: List[str] = []
    for cache_file, fname, ptype in zip(
        case["page_cache_files"], case["page_files"], case["page_types"]
    ):
        text = _page_verbatim_text(cache_file)
        parts.append(f"[page: {fname} | type: {ptype}]\n{text}")
    return "\n\n".join(parts)


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


def _consolidate_one(cfg: Config, provider: Provider, case: Dict[str, Any]) -> Optional[str]:
    max_attempts = int(cfg.get("run", "max_retries", default=5))
    init = float(cfg.get("run", "retry_initial_seconds", default=2))
    mx = float(cfg.get("run", "retry_max_seconds", default=60))

    transcripts = _assemble_transcripts(cfg, case)
    prompt = CONSOLIDATION_PROMPT.format(transcripts=transcripts)
    # full_transcript is assembled DETERMINISTICALLY in code (never from the
    # model), so it stays byte-identical to the per-page verbatim output.
    full_transcript = assemble_full_transcript(cfg, case)

    images: List[bytes] = []
    cover = case.get("cover_path")
    if cover and Path(cover).exists():
        try:
            images.append(prepare_image_bytes(Path(cover), cfg))
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
        interpreter=obj.get("interpreter"),
        plea_verbatim=obj.get("plea_verbatim"),
        verdict=obj.get("verdict"),
        full_transcript=full_transcript,
        language_notes=obj.get("language_notes"),
        field_confidence=field_confidence,
        uncertain_fields=list(obj.get("uncertain_fields") or []),
    )
    write_json(_case_out_path(cfg, case, rec.case_number), rec.model_dump())
    _mark_done(cfg, case)
    return None


def run_consolidate(
    cfg: Config,
    boxes: List[str] | None = None,
    limit: int | None = None,
    force: bool = False,
) -> Dict[str, int]:
    index = _load_cases_index(cfg)
    cfg.ensure_dirs()
    provider = get_provider_named(cfg, cfg.consolidate_provider)

    cases = index["cases"]
    if boxes:
        cases = [c for c in cases if c["box"] in boxes]

    todo = []
    stats = {"total": len(cases), "skipped_done": 0, "queued": 0, "errors": 0}
    for c in cases:
        if not force and _is_done(cfg, c):
            stats["skipped_done"] += 1
            continue
        todo.append(c)
        if limit and len(todo) >= limit:
            break
    stats["queued"] = len(todo)
    if not todo:
        return stats

    concurrency = int(cfg.get("run", "concurrency", default=6))
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_consolidate_one, cfg, provider, c): c for c in todo}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="cases"):
            if fut.result():
                stats["errors"] += 1
    return stats
