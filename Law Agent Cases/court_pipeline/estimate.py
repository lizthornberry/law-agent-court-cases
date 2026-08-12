"""Cost estimator / dry-run for the TWO-PASS pipeline.

Projects token usage and USD cost before a real run:
  * classify  : ALL transcribable images at the cheap classify-model price.
  * transcribe: only the NON-skip images, priced per ROUTED model.
  * consolidate: per case at the default (consolidation) model price.

The transcribe split needs a page-type distribution. Two sources:
  1. If a classify cache exists, count the ACTUAL page types per routed model.
  2. Otherwise fall back to the configurable assumptions in config [estimate]
     (assumed_skip_fraction + assumed_flash_transcribe_fraction).

Heuristic, not exact. Calibrate estimate.* / pricing.* after a small real run.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict

from .config import Config
from .inventory import load_manifest
from .util import read_json


def _cost(in_tokens: float, out_tokens: float, price: Dict[str, float], mult: float) -> float:
    return (in_tokens * price["input"] + out_tokens * price["output"]) / 1_000_000 * mult


def _count_classified_routes(cfg: Config, new_only: bool) -> Dict[str, int]:
    """Count cached, classified, non-skip pages bucketed by routed model.

    Returns {} if no usable classify cache exists. Also returns a "__skip__" key
    with the number of skipped pages so the caller can report it.
    """
    from .classify_transcribe import load_page_records

    skip_types = set(cfg.transcribe_skip_types)
    records = load_page_records(cfg)
    counts: Dict[str, int] = defaultdict(int)
    classified = 0
    for rec in records:
        if not rec.classified_at:
            continue
        classified += 1
        if rec.page_type in skip_types:
            counts["__skip__"] += 1
            continue
        counts[cfg.transcribe_model_for(rec.page_type)] += 1
    if classified == 0:
        return {}
    return dict(counts)


def estimate(cfg: Config, new_only: bool = False, use_batch: bool | None = None) -> Dict[str, Any]:
    manifest = load_manifest(cfg)
    est = cfg.get("estimate", default={}) or {}

    classify_in = float(est.get("classify_input_tokens_per_image",
                                est.get("input_tokens_per_image", 1100)))
    classify_out = float(est.get("classify_output_tokens_per_image", 150))
    trans_in = float(est.get("transcribe_input_tokens_per_image",
                             est.get("input_tokens_per_image", 1100)))
    trans_out = float(est.get("transcribe_output_tokens_per_image",
                              est.get("output_tokens_per_image", 800)))
    in_per_case = float(est.get("consolidation_input_tokens_per_case", 4500))
    # Output is now small: full_transcript is assembled in code, not emitted by
    # the model, so consolidation only returns the short structured fields.
    out_per_case = float(est.get("consolidation_output_tokens_per_case", 500))
    assumed_ipc = float(est.get("assumed_images_per_case", 7))
    batch_mult = float(est.get("batch_price_multiplier", 0.5))
    skip_frac = float(est.get("assumed_skip_fraction", 0.15))
    flash_frac = float(est.get("assumed_flash_transcribe_fraction", 0.10))

    skip_first = bool(cfg.get("run", "skip_first_image_in_box", default=True))
    classify_batch = cfg.stage_use_batch("classify", use_batch)
    transcribe_batch = cfg.stage_use_batch("transcribe", use_batch)
    classify_mult = batch_mult if classify_batch else 1.0
    transcribe_mult = batch_mult if transcribe_batch else 1.0

    # Count transcribable images (skip the per-box box photo).
    n_images = 0
    n_boxes = 0
    for box, items in manifest.get("boxes", {}).items():
        n_boxes += 1
        for it in items:
            if new_only and not it.get("is_new", False):
                continue
            if skip_first and it.get("is_first_in_box"):
                continue
            n_images += 1

    # ---- classify cost: every transcribable image at the classify model ----
    classify_price = cfg.price_for_model(cfg.classify_model)
    classify_cost = _cost(
        n_images * classify_in, n_images * classify_out, classify_price, classify_mult
    )

    # ---- transcribe cost: non-skip images, priced per routed model ---------
    routes = _count_classified_routes(cfg, new_only)
    transcribe_breakdown: Dict[str, Dict[str, Any]] = {}
    transcribe_cost = 0.0
    n_skip = 0
    n_transcribed = 0
    if routes:
        distribution_source = "actual (classify cache)"
        n_skip = routes.get("__skip__", 0)
        for model, n in routes.items():
            if model == "__skip__":
                continue
            price = cfg.price_for_model(model)
            c = _cost(n * trans_in, n * trans_out, price, transcribe_mult)
            transcribe_cost += c
            transcribe_breakdown[model] = {"n_images": n, "usd": round(c, 2)}
            n_transcribed += n
    else:
        distribution_source = (
            f"assumed (skip={skip_frac:.0%}, flash_of_nonskip={flash_frac:.0%})"
        )
        n_skip = int(round(n_images * skip_frac))
        n_nonskip = n_images - n_skip
        n_flash = int(round(n_nonskip * flash_frac))
        n_pro = n_nonskip - n_flash
        n_transcribed = n_nonskip
        # Flash bucket: cheapest distinct type_models price (if any), else default.
        type_models = cfg.transcribe_type_models
        flash_model = next(iter(type_models.values()), cfg.transcribe_default_model)
        pro_model = cfg.transcribe_default_model
        for model, n in ((flash_model, n_flash), (pro_model, n_pro)):
            if n <= 0:
                continue
            price = cfg.price_for_model(model)
            c = _cost(n * trans_in, n * trans_out, price, transcribe_mult)
            transcribe_cost += c
            b = transcribe_breakdown.setdefault(model, {"n_images": 0, "usd": 0.0})
            b["n_images"] += n
            b["usd"] = round(b["usd"] + c, 2)

    # ---- consolidation cost ------------------------------------------------
    n_cases = None
    if cfg.cases_index_path.exists():
        try:
            n_cases = read_json(cfg.cases_index_path).get("n_cases")
        except Exception:
            n_cases = None
    if not n_cases:
        n_cases = max(1, round(n_images / assumed_ipc)) if n_images else 0
    cons_price = cfg.price_for_model(cfg.consolidate_model)
    cons_in = n_cases * in_per_case
    cons_out = n_cases * out_per_case
    # Consolidation is text-heavy; usually run live regardless -> full price.
    cons_cost = _cost(cons_in, cons_out, cons_price, 1.0)

    total = classify_cost + transcribe_cost + cons_cost
    return {
        "provider": cfg.provider,
        "modes": {
            "classify": "batch" if classify_batch else "live",
            "transcribe": "batch" if transcribe_batch else "live",
            "consolidation": "live",
        },
        "new_only": new_only,
        "n_boxes": n_boxes,
        "n_images_to_classify": n_images,
        "n_images_skipped_transcribe": n_skip,
        "n_images_transcribed": n_transcribed,
        "n_cases_estimated": n_cases,
        "models": {
            "classify": cfg.classify_model,
            "transcribe_default": cfg.transcribe_default_model,
            "transcribe_overrides": cfg.transcribe_type_models,
            "consolidate": cfg.consolidate_model,
        },
        "transcribe_distribution_source": distribution_source,
        "transcribe_breakdown": transcribe_breakdown,
        "usd": {
            "classify": round(classify_cost, 2),
            "transcribe": round(transcribe_cost, 2),
            "consolidation": round(cons_cost, 2),
            "total": round(total, 2),
        },
        "note": "Heuristic estimate. Calibrate estimate.* / pricing.* after a small real run.",
    }


def format_report(r: Dict[str, Any]) -> str:
    lines = [
        "Cost estimate (heuristic, two-pass)",
        "===================================",
        f"provider       : {r['provider']}",
        f"modes          : classify={r['modes']['classify']} | "
        f"transcribe={r['modes']['transcribe']} | "
        f"consolidation={r['modes']['consolidation']}",
        f"models         : classify={r['models']['classify']} | "
        f"transcribe_default={r['models']['transcribe_default']} | "
        f"consolidate={r['models']['consolidate']}",
        f"  overrides    : {r['models']['transcribe_overrides'] or '{}'}",
        f"new_only       : {r['new_only']}",
        f"boxes          : {r['n_boxes']}",
        f"images classify: {r['n_images_to_classify']:,}",
        f"images transcr.: {r['n_images_transcribed']:,} "
        f"(skipped {r['n_images_skipped_transcribe']:,})",
        f"cases (est.)   : {r['n_cases_estimated']:,}",
        f"distribution   : {r['transcribe_distribution_source']}",
        "",
        f"classify       : ${r['usd']['classify']:,.2f}",
        f"transcribe     : ${r['usd']['transcribe']:,.2f}",
    ]
    for model, b in r.get("transcribe_breakdown", {}).items():
        lines.append(f"    {model:<28} {b['n_images']:>6,} img  ${b['usd']:,.2f}")
    lines += [
        f"consolidation  : ${r['usd']['consolidation']:,.2f}",
        f"TOTAL          : ${r['usd']['total']:,.2f}",
        "",
        r["note"],
    ]
    return "\n".join(lines)
