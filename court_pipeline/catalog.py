"""Build the SQLite catalog and derived exports (index.json, review.csv) from case JSONs."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from .config import Config
from .schema import CASE_FIELDS, CaseRecord
from .util import now_iso, read_json, write_json

SCALAR_COLUMNS = [
    "case_id", "box", "is_appeal",
    "case_number", "district", "magistrate", "plaintiff", "defendant", "claim",
    "date_heard", "date_heard_iso", "appearance_for_plaintiff",
    "lawyer_or_agent_for_plaintiff", "lawyer_or_agent_for_defendant", "interpreter",
    "verdict", "language_notes", "provider", "model", "processed_at", "error",
    "n_pages", "page_start", "page_end", "source_json",
]


def _load_case_jsons(cfg: Config) -> List[Dict[str, Any]]:
    out = []
    if not cfg.cases_out_dir.exists():
        return out
    for jf in sorted(cfg.cases_out_dir.glob("*.json")):
        try:
            data = read_json(jf)
            data["__source_json"] = str(jf)
            out.append(data)
        except Exception:
            continue
    return out


def build_catalog(cfg: Config) -> Dict[str, Any]:
    cfg.ensure_dirs()
    cases = _load_case_jsons(cfg)

    db_path = cfg.output_dir / "catalog.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cols_sql = ", ".join(f'"{c}" TEXT' for c in SCALAR_COLUMNS)
    cur.execute(f"CREATE TABLE cases ({cols_sql})")

    index_rows: List[Dict[str, Any]] = []
    review_rows: List[Dict[str, Any]] = []

    for data in cases:
        rec = CaseRecord(**{k: v for k, v in data.items() if not k.startswith("__")})
        page_range = rec.page_range or []
        row = {
            "case_id": rec.case_id,
            "box": rec.box,
            "is_appeal": "1" if rec.is_appeal else "0",
            "case_number": rec.case_number,
            "district": rec.district,
            "magistrate": rec.magistrate,
            "plaintiff": rec.plaintiff,
            "defendant": rec.defendant,
            "claim": rec.claim,
            "date_heard": rec.date_heard,
            "date_heard_iso": rec.date_heard_iso,
            "appearance_for_plaintiff": rec.appearance_for_plaintiff,
            "lawyer_or_agent_for_plaintiff": rec.lawyer_or_agent_for_plaintiff,
            "lawyer_or_agent_for_defendant": rec.lawyer_or_agent_for_defendant,
            "interpreter": rec.interpreter,
            "verdict": rec.verdict,
            "language_notes": rec.language_notes,
            "provider": rec.provider,
            "model": rec.model,
            "processed_at": rec.processed_at,
            "error": rec.error,
            "n_pages": str(len(rec.source_images)),
            "page_start": str(page_range[0]) if page_range else None,
            "page_end": str(page_range[1]) if len(page_range) > 1 else None,
            "source_json": data.get("__source_json"),
        }
        cur.execute(
            f'INSERT INTO cases ({", ".join(chr(34)+c+chr(34) for c in SCALAR_COLUMNS)}) '
            f'VALUES ({", ".join("?" for _ in SCALAR_COLUMNS)})',
            [row.get(c) for c in SCALAR_COLUMNS],
        )
        index_rows.append(row)

        # Review rows: anything missing or low-confidence.
        missing = [f for f in CASE_FIELDS if not getattr(rec, f, None)]
        low = [f for f, v in (rec.field_confidence or {}).items() if str(v).lower() == "low"]
        flagged = sorted(set(missing) | set(low) | set(rec.uncertain_fields or []))
        if rec.error:
            flagged = ["ERROR"] + flagged
        if flagged:
            review_rows.append(
                {
                    "case_id": rec.case_id,
                    "box": rec.box,
                    "case_number": rec.case_number or "",
                    "source_json": data.get("__source_json", ""),
                    "missing_or_low_confidence_fields": "; ".join(flagged),
                    "error": rec.error or "",
                }
            )

    conn.commit()
    conn.close()

    # index.json
    write_json(
        cfg.output_dir / "index.json",
        {"generated_at": now_iso(), "n_cases": len(index_rows), "cases": index_rows},
    )

    # review.csv
    review_path = cfg.output_dir / "review.csv"
    with open(review_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "case_id", "box", "case_number", "missing_or_low_confidence_fields",
                "error", "source_json",
            ],
        )
        writer.writeheader()
        for r in review_rows:
            writer.writerow(r)

    return {
        "n_cases": len(index_rows),
        "n_review": len(review_rows),
        "db": str(db_path),
        "index": str(cfg.output_dir / "index.json"),
        "review": str(review_path),
    }
