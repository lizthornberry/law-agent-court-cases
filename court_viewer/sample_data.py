"""Generate a small SAMPLE ``results.json`` for offline testing (no API calls).

This synthesizes a handful of representative cases that match the court_viewer
schema exactly (see :mod:`court_viewer.viewer_schema`). Pages reference REAL
image filenames under a real box folder so the image/thumbnail endpoints can be
exercised against actual files on disk.

The sample demonstrates every interesting slot:
  * gemini-only fields (the common v1 case),
  * a pre-existing ``edited`` override (effective value = edited),
  * a populated ``claude`` alternate (must be PRESERVED but NOT indexed for search),
  * all four review statuses, and a case with notes.

It writes to an ISOLATED path by default and never touches court_pipeline data.

Usage::

    python -m court_viewer.sample_data [--output path] [--box BOX]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import load_config
from .viewer_schema import SCHEMA_VERSION, normalize_case

# A real box that exists under archive_root, with these real image files.
DEFAULT_BOX = "1-NKE 2-1-1-11"


def _tri(gemini=None, claude=None, edited=None) -> Dict[str, Any]:
    return {"gemini": gemini, "claude": claude, "edited": edited}


def _case(
    case_id: str,
    box: str,
    fields: Dict[str, Any],
    pages: List[Dict[str, Any]],
    review_status: str = "unreviewed",
    notes: str = "",
    page_range: Optional[List[int]] = None,
    field_confidence: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    source_images = [p["filename"] for p in pages]
    return normalize_case(
        {
            "case_id": case_id,
            "box": box,
            "is_appeal": False,
            "page_range": page_range or [1, len(pages)],
            "review_status": review_status,
            "notes": notes,
            "source_images": source_images,
            "fields": fields,
            "pages": pages,
            "field_confidence": field_confidence or {},
            "provenance": {
                "provider": "gemini",
                "model": "gemini-3.1-pro-preview",
                "processed_at": "2026-06-24T17:08:32-04:00",
            },
        }
    )


def build_sample_doc(box: str = DEFAULT_BOX) -> Dict[str, Any]:
    cases: List[Dict[str, Any]] = []

    # Case 1: gemini-only, verified, with confidence and notes.
    cases.append(
        _case(
            case_id=f"{box}__sample_001",
            box=box,
            review_status="verified",
            notes="Defamation case; isiXhosa quote in the summons.",
            page_range=[28, 30],
            field_confidence={"interpreter": "medium", "plaintiff": "high"},
            fields={
                "case_number": _tri(gemini="25/91"),
                "district": _tri(gemini="Nqamakwe"),
                "magistrate": _tri(gemini="John T. O'Connor"),
                "plaintiff": _tri(gemini="Mhlini"),
                "defendant": _tri(gemini="Bali"),
                "claim": _tri(gemini="£9.0.0 for damages for defamation of character"),
                "date_heard": _tri(gemini="18th August 1891"),
                "date_heard_iso": _tri(gemini="1891-08-18"),
                "appearance_for_plaintiff": _tri(gemini="In person"),
                "interpreter": _tri(gemini="A. M. [illegible: Fainse?]"),
                "plea_verbatim": _tri(gemini="Not liable."),
                "verdict": _tri(gemini=None),
                "full_transcript": _tri(
                    gemini="Civil Case No 25/91\nIn the Court of the Resident Magistrate "
                    "for the District of Nqamakwe held at Nqamakwe this 18th August 1891 "
                    "Before John T. O'Connor Esq. Mhlini versus Bali. Claim for £9.0.0. "
                    "Plea: Not liable."
                ),
            },
            pages=[
                {"filename": "IMG_6402.jpg", "order": 28, "page_type": "cover_regular",
                 "transcript": _tri(gemini="Civil Case No 25/91 — cover page. Mhlini versus Bali.")},
                {"filename": "IMG_6403.jpg", "order": 29, "page_type": "hearing",
                 "transcript": _tri(gemini="Mhlini duly cautioned states: I am plaintiff in this case.")},
                {"filename": "IMG_6404.jpg", "order": 30, "page_type": "other",
                 "transcript": _tri(gemini="Summons served on Bali by J. Curnick, Messenger of the Court.")},
            ],
        )
    )

    # Case 2: has a pre-existing EDITED override + a CLAUDE alternate (not indexed).
    cases.append(
        _case(
            case_id=f"{box}__sample_002",
            box=box,
            review_status="in progress",
            notes="Plaintiff name corrected by reviewer.",
            page_range=[1, 2],
            field_confidence={"plaintiff": "low"},
            fields={
                "case_number": _tri(gemini="71/91"),
                "district": _tri(gemini="Nqamakwe"),
                "magistrate": _tri(gemini="John T. O'Connor"),
                # effective = edited "Silanda" (gemini misread "Silonda"); claude alt present.
                "plaintiff": _tri(gemini="Silonda", claude="Silander", edited="Silanda"),
                "defendant": _tri(gemini="Mtshwana"),
                "claim": _tri(gemini="£9 - or Value to that amount as per summons"),
                "date_heard": _tri(gemini="5th December 1890"),
                "date_heard_iso": _tri(gemini="1890-12-05"),
                "appearance_for_plaintiff": _tri(gemini="Mr Kelly"),
                "interpreter": _tri(gemini="J M Njamela"),
                "plea_verbatim": _tri(gemini=None),
                "verdict": _tri(gemini=None),
                "full_transcript": _tri(
                    gemini="Civil Case No 71/91. Silonda (Married) Plaintiff versus "
                    "Mtshwana (Married) Defendant. Claim for £9.",
                    claude="Civil Case No 71/91. Silander, Married, Plaintiff vs Mtshwana, "
                    "Married, Defendant. Claim for nine pounds.",
                ),
            },
            pages=[
                {"filename": "IMG_6375.jpg", "order": 1, "page_type": "cover_regular",
                 "transcript": _tri(
                     gemini="Civil Case No 71/91 [Stamp: RESIDENT MAGISTRATE 5 DEC. 90 NQAMAKWE]. "
                     "Before John T. O'Connor Esqr. Silonda versus Mtshwana.",
                     # claude alternate for the transcript — preserved, never searched.
                     claude="Civil Case No 71/91. Before John T O'Connor. Silander vs Mtshwana.")},
                {"filename": "IMG_6376.jpg", "order": 2, "page_type": "hearing",
                 "transcript": _tri(gemini="Mr Kelly asks that the summons be amended to insert the "
                                    "defendant's father's name, Sogabu, as the defendant is a minor.")},
            ],
        )
    )

    # Case 3: gemini-only, flagged, sparse fields.
    cases.append(
        _case(
            case_id=f"{box}__sample_003",
            box=box,
            review_status="flagged",
            notes="Page 1 partially illegible — needs a second look.",
            page_range=[3, 3],
            fields={
                "case_number": _tri(gemini="109/91"),
                "district": _tri(gemini="Nqamakwe"),
                "magistrate": _tri(gemini=None),
                "plaintiff": _tri(gemini="Nomtshato"),
                "defendant": _tri(gemini="Mgudlwa"),
                "claim": _tri(gemini="Restitution of cattle"),
                "date_heard": _tri(gemini=None),
                "date_heard_iso": _tri(gemini=None),
                "appearance_for_plaintiff": _tri(gemini=None),
                "interpreter": _tri(gemini=None),
                "plea_verbatim": _tri(gemini=None),
                "verdict": _tri(gemini=None),
                "full_transcript": _tri(gemini="Claim for restitution of cattle. Defendant absent."),
            },
            pages=[
                {"filename": "IMG_6377.jpg", "order": 3, "page_type": "cover_regular",
                 "transcript": _tri(gemini="Civil Case No 109/91. Nomtshato versus Mgudlwa. "
                                    "Claim for restitution of cattle.")},
            ],
        )
    )

    # Case 4: completely unreviewed, default everything.
    cases.append(
        _case(
            case_id=f"{box}__sample_004",
            box=box,
            review_status="unreviewed",
            page_range=[4, 4],
            fields={
                "case_number": _tri(gemini="113/91"),
                "district": _tri(gemini="Nqamakwe"),
                "magistrate": _tri(gemini="John T. O'Connor"),
                "plaintiff": _tri(gemini="Tshaka"),
                "defendant": _tri(gemini="Ndabeni"),
                "claim": _tri(gemini="£5 for breach of contract"),
                "date_heard": _tri(gemini="2nd September 1891"),
                "date_heard_iso": _tri(gemini="1891-09-02"),
                "appearance_for_plaintiff": _tri(gemini="In person"),
                "interpreter": _tri(gemini=None),
                "plea_verbatim": _tri(gemini="Admits liability."),
                "verdict": _tri(gemini="Judgment for plaintiff with costs."),
                "full_transcript": _tri(gemini="Civil Case No 113/91. Tshaka versus Ndabeni. "
                                        "Claim £5 for breach of contract. Judgment for plaintiff."),
            },
            pages=[
                {"filename": "IMG_6378.jpg", "order": 4, "page_type": "cover_regular",
                 "transcript": _tri(gemini="Civil Case No 113/91. Tshaka versus Ndabeni.")},
            ],
        )
    )

    cfg = load_config()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "archive_root": str(cfg.archive_root),
        "cases": cases,
    }


def write_sample(output: Path, box: str = DEFAULT_BOX) -> Path:
    doc = build_sample_doc(box=box)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    return output


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a sample results.json for offline testing.")
    parser.add_argument("--output", default=None, help="Output path (default: court_viewer/sample/results.sample.json)")
    parser.add_argument("--box", default=DEFAULT_BOX, help="Real box folder to reference for images")
    args = parser.parse_args(argv)

    out = Path(args.output).resolve() if args.output else (Path(__file__).resolve().parent / "sample" / "results.sample.json")
    write_sample(out, box=args.box)
    print(f"Wrote sample results.json -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
