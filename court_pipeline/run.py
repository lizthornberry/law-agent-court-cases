"""Command-line entrypoint for the court case transcription pipeline.

The per-image work is TWO passes: `classify` (cheap/fast triage) then
`transcribe` (a model routed by page_type does the verbatim transcription).
Both write into the same data/pages/<box>/<file>.json. `pages` runs both.

Usage (from the directory ABOVE court_pipeline/):
    python -m court_pipeline.run inventory
    python -m court_pipeline.run estimate    [--new-only] [--batch]
    python -m court_pipeline.run classify    [--box NAME ...] [--limit N] [--new-only] [--batch] [--force]
    python -m court_pipeline.run segment     [--box NAME ...]
    python -m court_pipeline.run transcribe  [--box NAME ...] [--limit N] [--new-only] [--batch|--live] [--force]
    python -m court_pipeline.run pages       [--box NAME ...] [--limit N] [--new-only] [--batch] [--force]
    python -m court_pipeline.run cases       [--box NAME ...] [--limit N] [--force]
    python -m court_pipeline.run catalog
    python -m court_pipeline.run all         [--box NAME ...] [--limit N] [--new-only] [--batch] [--force]

Per-stage batch defaults (config ``stages.<pass>.mode``): transcribe defaults to
batch; classify defaults to live. ``--batch`` forces batch; ``--live`` forces live
(overrides stage batch mode for this run only).

Stage dependencies:
    classify   <- inventory
    segment    <- classify
    transcribe <- classify
    cases      <- transcribe + segment
    catalog    <- cases
`all` order: inventory -> classify -> segment -> transcribe -> cases -> catalog
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .config import load_config


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _boxes_arg(args) -> Optional[List[str]]:
    return args.box if getattr(args, "box", None) else None


def _use_batch_arg(args) -> bool | None:
    """Resolve CLI batch/live override for classify+transcribe passes."""
    if getattr(args, "live", False):
        return False
    if getattr(args, "batch", False):
        return True
    return None


def cmd_inventory(args, cfg):
    from .inventory import build_manifest

    m = build_manifest(cfg)
    print(
        f"Inventory: {m['n_boxes']} boxes, {m['n_images']} images "
        f"({m['n_new']} new). Manifest: {cfg.manifest_path}"
    )


def cmd_estimate(args, cfg):
    from .estimate import estimate, format_report

    r = estimate(cfg, new_only=args.new_only, use_batch=(True if args.batch else None))
    print(format_report(r))


def cmd_classify(args, cfg):
    from .classify_transcribe import run_classify

    stats = run_classify(
        cfg,
        boxes=_boxes_arg(args),
        limit=args.limit,
        new_only=args.new_only,
        force=args.force,
        use_batch=_use_batch_arg(args),
    )
    print("Classify pass (A):")
    _print_json(stats)


def cmd_transcribe(args, cfg):
    from .classify_transcribe import run_transcribe

    stats = run_transcribe(
        cfg,
        boxes=_boxes_arg(args),
        limit=args.limit,
        new_only=args.new_only,
        force=args.force,
        use_batch=_use_batch_arg(args),
    )
    print("Transcribe pass (B):")
    _print_json(stats)


def cmd_pages(args, cfg):
    from .classify_transcribe import run_pages

    stats = run_pages(
        cfg,
        boxes=_boxes_arg(args),
        limit=args.limit,
        new_only=args.new_only,
        force=args.force,
        use_batch=_use_batch_arg(args),
    )
    print("Pages (classify + transcribe):")
    _print_json(stats)


def cmd_segment(args, cfg):
    from .segment import segment_cases

    out = segment_cases(cfg, boxes=_boxes_arg(args))
    print(
        f"Segmented {out['n_cases']} cases "
        f"({out['n_appeals']} appeals, {out['n_needs_review']} need review). "
        f"-> {cfg.cases_index_path}"
    )


def cmd_cases(args, cfg):
    from .consolidate import run_consolidate

    stats = run_consolidate(cfg, boxes=_boxes_arg(args), limit=args.limit, force=args.force)
    print("Consolidation:")
    _print_json(stats)


def cmd_catalog(args, cfg):
    from .catalog import build_catalog

    out = build_catalog(cfg)
    print("Catalog built:")
    _print_json(out)


def cmd_all(args, cfg):
    from .classify_transcribe import run_classify, run_transcribe
    from .consolidate import run_consolidate
    from .catalog import build_catalog
    from .inventory import build_manifest
    from .segment import segment_cases

    boxes = _boxes_arg(args)
    use_batch = _use_batch_arg(args)
    print("[1/6] inventory ...")
    build_manifest(cfg)
    print("[2/6] classify ...")
    _print_json(
        run_classify(
            cfg, boxes=boxes, limit=args.limit, new_only=args.new_only,
            force=args.force, use_batch=use_batch,
        )
    )
    print("[3/6] segment ...")
    seg = segment_cases(cfg, boxes=boxes)
    print(f"   {seg['n_cases']} cases")
    print("[4/6] transcribe ...")
    _print_json(
        run_transcribe(
            cfg, boxes=boxes, limit=args.limit, new_only=args.new_only,
            force=args.force, use_batch=use_batch,
        )
    )
    print("[5/6] cases (consolidate) ...")
    _print_json(run_consolidate(cfg, boxes=boxes, limit=args.limit, force=args.force))
    print("[6/6] catalog ...")
    _print_json(build_catalog(cfg))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="court_pipeline", description=__doc__)
    p.add_argument("--config", default=None, help="path to config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp, box=True, limit=True, new_only=True, batch=True, force=True):
        if box:
            sp.add_argument("--box", action="append", help="restrict to box name (repeatable)")
        if limit:
            sp.add_argument("--limit", type=int, default=None, help="max items to process")
        if new_only:
            sp.add_argument("--new-only", dest="new_only", action="store_true",
                            help="only newly added/changed images")
        if batch:
            mode = sp.add_mutually_exclusive_group()
            mode.add_argument(
                "--batch", action="store_true",
                help="force async batch mode for classify+transcribe (overrides per-stage defaults)",
            )
            mode.add_argument(
                "--live", action="store_true",
                help="force live (sync) mode for classify+transcribe (overrides stage batch mode)",
            )
        if force:
            sp.add_argument("--force", action="store_true", help="reprocess even if cached")

    sp = sub.add_parser("inventory", help="build/update the image manifest")
    sp.set_defaults(func=cmd_inventory)

    sp = sub.add_parser("estimate", help="estimate token cost (dry run)")
    sp.add_argument("--new-only", dest="new_only", action="store_true")
    sp.add_argument("--batch", action="store_true")
    sp.set_defaults(func=cmd_estimate)

    sp = sub.add_parser("classify", help="Pass A: cheap/fast page-type triage")
    add_common(sp)
    sp.set_defaults(func=cmd_classify)

    sp = sub.add_parser("transcribe", help="Pass B: routed verbatim transcription")
    add_common(sp)
    sp.set_defaults(func=cmd_transcribe)

    sp = sub.add_parser("pages", help="run classify then transcribe (convenience)")
    add_common(sp)
    sp.set_defaults(func=cmd_pages)

    sp = sub.add_parser("segment", help="group pages into cases (needs classify)")
    add_common(sp, limit=False, new_only=False, batch=False, force=False)
    sp.set_defaults(func=cmd_segment)

    sp = sub.add_parser("cases", help="consolidate each case into structured JSON")
    add_common(sp, new_only=False, batch=False)
    sp.set_defaults(func=cmd_cases)

    sp = sub.add_parser("catalog", help="build SQLite catalog + index.json + review.csv")
    sp.set_defaults(func=cmd_catalog)

    sp = sub.add_parser("all", help="run the full pipeline end to end")
    add_common(sp)
    sp.set_defaults(func=cmd_all)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    args.func(args, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
