"""Group page records into individual cases.

A new case starts when:
  * a cover page (cover_regular / cover_appeal) is seen, OR
  * the margin case number changes (configurable), OR
  * the box changes.
box_photo / blank pages are attached to the current case but never start one.
Pages appearing before the first cover in a box still form a case (flagged for review)
so nothing is dropped.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .classify_transcribe import load_page_records
from .config import Config
from .schema import PageRecord
from .util import now_iso, write_json


def _norm_case_no(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value.strip().strip(".").replace(" ", "")


def segment_cases(cfg: Config, boxes: List[str] | None = None) -> Dict[str, Any]:
    records = load_page_records(cfg, boxes)
    cover_types = set(cfg.get("segment", "cover_types", default=["cover_regular", "cover_appeal"]))
    split_on_margin = bool(cfg.get("segment", "split_on_margin_case_number_change", default=True))

    cases: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    seq_in_box = 0
    last_box: Optional[str] = None
    current_margin: Optional[str] = None

    def start_case(rec: PageRecord, reason: str) -> Dict[str, Any]:
        nonlocal seq_in_box
        seq_in_box += 1
        return {
            "case_id": f"{rec.box}__{seq_in_box:03d}",
            "box": rec.box,
            "is_appeal": rec.page_type == "cover_appeal",
            "start_reason": reason,
            "provisional_case_number": _norm_case_no(rec.margin_case_number),
            "cover_filename": rec.filename if rec.page_type in cover_types else None,
            "cover_path": rec.path if rec.page_type in cover_types else None,
            "page_files": [],
            "page_paths": [],
            "page_cache_files": [],
            "orders": [],
            "page_types": [],
            "needs_review": rec.page_type not in cover_types,
        }

    for rec in records:
        if rec.page_type == "box_photo":
            # Resets box context but contributes nothing.
            if rec.box != last_box:
                last_box = rec.box
                seq_in_box = 0
                current = None
                current_margin = None
            continue

        margin = _norm_case_no(rec.margin_case_number)

        new_box = rec.box != last_box
        if new_box:
            last_box = rec.box
            seq_in_box = 0
            current = None
            current_margin = None

        is_cover = rec.page_type in cover_types
        margin_changed = (
            split_on_margin
            and margin is not None
            and current_margin is not None
            and margin != current_margin
        )

        if current is None or is_cover or margin_changed:
            reason = "cover" if is_cover else ("margin_change" if margin_changed else "box_start")
            current = start_case(rec, reason)
            cases.append(current)
            current_margin = margin

        if margin is not None:
            current_margin = margin
            if not current.get("provisional_case_number"):
                current["provisional_case_number"] = margin
        if is_cover and current.get("cover_filename") is None:
            current["cover_filename"] = rec.filename
            current["cover_path"] = rec.path
        if rec.page_type == "cover_appeal":
            current["is_appeal"] = True

        current["page_files"].append(rec.filename)
        current["page_paths"].append(rec.path)
        current["page_cache_files"].append(
            str(cfg.pages_dir / rec.box / f"{rec.filename}.json")
        )
        current["orders"].append(rec.order)
        current["page_types"].append(rec.page_type)

    # Finalize page_range.
    for c in cases:
        if c["orders"]:
            c["page_range"] = [min(c["orders"]), max(c["orders"])]
        else:
            c["page_range"] = []
        # Cover defaults to first page if no explicit cover detected.
        if not c.get("cover_path") and c["page_paths"]:
            c["cover_path"] = c["page_paths"][0]
            c["cover_filename"] = c["page_files"][0]

    out = {
        "generated_at": now_iso(),
        "n_cases": len(cases),
        "n_appeals": sum(1 for c in cases if c["is_appeal"]),
        "n_needs_review": sum(1 for c in cases if c.get("needs_review")),
        "cases": cases,
    }
    cfg.ensure_dirs()
    write_json(cfg.cases_index_path, out)
    return out
