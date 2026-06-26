"""Shared constants + helpers describing the court_viewer ``results.json`` schema.

results.json is the CANONICAL, portable source of truth. Shape::

    {
      "schema_version": 1,
      "generated_at": "<iso8601>",
      "archive_root": "<abs path>",
      "cases": [ <case>, ... ]
    }

Each <case>::

    {
      "case_id", "box", "is_appeal", "page_range",
      "review_status": "unreviewed",   # one of REVIEW_STATUSES
      "notes": {"gemini": null, "edited": null},  # user annotations only
      "source_images": ["DSC03866.jpg", ...],
      "fields": {                      # tri-value object per field
        "<field>": {"gemini": <str|null>, "claude": <str|null>, "edited": <str|null>},
        ...
      },
      "pages": [
        {"filename", "order", "page_type",
         "transcript": {"gemini": <str|null>, "claude": <str|null>, "edited": <str|null>}}
      ],
      "field_confidence": {...},
      "provenance": {"provider", "model", "processed_at"}
    }

The EFFECTIVE (current) value of any field/transcript = ``edited`` if non-null
else ``gemini``. ``claude`` is the ALTERNATE (future compare view) and is NOT
indexed for search.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

# Extracted case-level fields exposed as tri-value objects, in display order.
# (Mirrors court_pipeline CaseRecord requested fields + date_heard_iso.)
FIELD_NAMES: List[str] = [
    "case_number",
    "district",
    "magistrate",
    "plaintiff",
    "defendant",
    "claim",
    "date_heard",
    "date_heard_iso",
    "appearance_for_plaintiff",
    "lawyer_or_agent_for_plaintiff",
    "lawyer_or_agent_for_defendant",
    "interpreter",
    "plea_verbatim",
    "verdict",
    "full_transcript",
]

# Fixed review-status set.
REVIEW_STATUSES: List[str] = ["unreviewed", "in progress", "verified", "flagged"]
DEFAULT_REVIEW_STATUS = "unreviewed"

# The three slots of a tri-value object.
TRI_SLOTS = ("gemini", "claude", "edited")


def empty_trivalue() -> Dict[str, Optional[str]]:
    return {"gemini": None, "claude": None, "edited": None}


def effective(tri: Dict[str, Any]) -> Optional[str]:
    """Effective value: edited if non-null else gemini."""
    if not isinstance(tri, dict):
        return None
    edited = tri.get("edited")
    if edited is not None:
        return edited
    return tri.get("gemini")


def normalize_trivalue(tri: Any) -> Dict[str, Optional[str]]:
    """Coerce arbitrary input into a well-formed tri-value object."""
    out = empty_trivalue()
    if isinstance(tri, dict):
        for slot in TRI_SLOTS:
            val = tri.get(slot)
            out[slot] = val if (val is None or isinstance(val, str)) else str(val)
    return out


def normalize_notes(notes: Any) -> Dict[str, Optional[str]]:
    """User-owned reviewer notes. ``gemini`` is always null (pipeline must not fill)."""
    if isinstance(notes, str):
        out = empty_trivalue()
        out["edited"] = notes if notes else None
        return out
    tri = normalize_trivalue(notes)
    tri["gemini"] = None
    return tri


def normalize_case(case: Dict[str, Any]) -> Dict[str, Any]:
    """Return a well-formed case dict, filling defaults for missing keys."""
    fields_in = case.get("fields") or {}
    fields_out: Dict[str, Any] = {}
    for name in FIELD_NAMES:
        fields_out[name] = normalize_trivalue(fields_in.get(name))

    pages_out: List[Dict[str, Any]] = []
    for p in case.get("pages") or []:
        pages_out.append(
            {
                "filename": p.get("filename", ""),
                "order": int(p.get("order", 0) or 0),
                "page_type": p.get("page_type", "other") or "other",
                "transcript": normalize_trivalue(p.get("transcript")),
            }
        )

    status = case.get("review_status") or DEFAULT_REVIEW_STATUS
    if status not in REVIEW_STATUSES:
        status = DEFAULT_REVIEW_STATUS

    return {
        "case_id": case.get("case_id", ""),
        "box": case.get("box", ""),
        "is_appeal": bool(case.get("is_appeal", False)),
        "page_range": list(case.get("page_range") or []),
        "review_status": status,
        "notes": normalize_notes(case.get("notes")),
        "source_images": list(case.get("source_images") or []),
        "fields": fields_out,
        "pages": pages_out,
        "field_confidence": dict(case.get("field_confidence") or {}),
        "provenance": dict(case.get("provenance") or {}),
    }
