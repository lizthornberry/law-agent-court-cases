"""SQLite (+FTS5) working store for court_viewer.

SQLite is a LOCAL, disposable working store. ``results.json`` is canonical:

* :func:`rebuild_from_results` (re)builds the DB from results.json.
* :func:`is_stale` / :func:`ensure_fresh` rebuild when results.json is newer
  than the DB's stored sync token (or the DB is missing).
* :func:`export_results` writes results.json back out from the DB after edits
  and re-syncs the token so the export is not mistaken for staleness.

Search uses an FTS5 table over EFFECTIVE field values, EFFECTIVE page
transcripts, and notes. ``claude`` alternates are intentionally NOT indexed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config
from .viewer_schema import (
    FIELD_NAMES,
    REVIEW_STATUSES,
    SCHEMA_VERSION,
    effective,
    normalize_case,
)

# Field tokens usable as a search scope, in addition to the FIELD_NAMES above.
SEARCH_SCOPE_TRANSCRIPT = "transcript"
SEARCH_SCOPE_NOTES = "notes"
SEARCH_SCOPES = FIELD_NAMES + [SEARCH_SCOPE_TRANSCRIPT, SEARCH_SCOPE_NOTES]


# --------------------------------------------------------------------------
# connection + schema
# --------------------------------------------------------------------------
def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS cases (
            case_id        TEXT PRIMARY KEY,
            box            TEXT,
            is_appeal      INTEGER,
            review_status  TEXT,
            -- effective scalar values cached for fast list/sort:
            case_number    TEXT,
            plaintiff      TEXT,
            defendant      TEXT,
            district       TEXT,
            date_heard     TEXT,
            page_count     INTEGER,
            -- full canonical case object as JSON:
            data           TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_cases_box ON cases(box);
        CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(review_status);

        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
            case_id UNINDEXED,
            field   UNINDEXED,
            content,
            tokenize = 'unicode61'
        );
        """
    )
    conn.commit()


# --------------------------------------------------------------------------
# meta helpers
# --------------------------------------------------------------------------
def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


# --------------------------------------------------------------------------
# indexing
# --------------------------------------------------------------------------
def _index_case(conn: sqlite3.Connection, case: Dict[str, Any]) -> None:
    """(Re)build FTS rows for a single case from its EFFECTIVE values + notes."""
    case_id = case["case_id"]
    conn.execute("DELETE FROM fts WHERE case_id=?", (case_id,))

    rows: List[tuple] = []
    for name in FIELD_NAMES:
        val = effective(case["fields"].get(name) or {})
        if val:
            rows.append((case_id, name, val))

    for page in case.get("pages", []):
        val = effective(page.get("transcript") or {})
        if val:
            rows.append((case_id, SEARCH_SCOPE_TRANSCRIPT, val))

    notes = case.get("notes") or ""
    if notes.strip():
        rows.append((case_id, SEARCH_SCOPE_NOTES, notes))

    if rows:
        conn.executemany("INSERT INTO fts(case_id, field, content) VALUES(?,?,?)", rows)


def _upsert_case(conn: sqlite3.Connection, case: Dict[str, Any]) -> None:
    case = normalize_case(case)
    conn.execute(
        """
        INSERT INTO cases(case_id, box, is_appeal, review_status, case_number,
                          plaintiff, defendant, district, date_heard, page_count, data)
        VALUES(:case_id, :box, :is_appeal, :review_status, :case_number,
               :plaintiff, :defendant, :district, :date_heard, :page_count, :data)
        ON CONFLICT(case_id) DO UPDATE SET
            box=excluded.box, is_appeal=excluded.is_appeal,
            review_status=excluded.review_status, case_number=excluded.case_number,
            plaintiff=excluded.plaintiff, defendant=excluded.defendant,
            district=excluded.district, date_heard=excluded.date_heard,
            page_count=excluded.page_count, data=excluded.data
        """,
        {
            "case_id": case["case_id"],
            "box": case["box"],
            "is_appeal": 1 if case["is_appeal"] else 0,
            "review_status": case["review_status"],
            "case_number": effective(case["fields"].get("case_number") or {}),
            "plaintiff": effective(case["fields"].get("plaintiff") or {}),
            "defendant": effective(case["fields"].get("defendant") or {}),
            "district": effective(case["fields"].get("district") or {}),
            "date_heard": effective(case["fields"].get("date_heard") or {}),
            "page_count": len(case.get("pages", [])),
            "data": json.dumps(case, ensure_ascii=False),
        },
    )
    _index_case(conn, case)


# --------------------------------------------------------------------------
# rebuild / staleness / export
# --------------------------------------------------------------------------
def _results_mtime(results_path: Path) -> Optional[float]:
    try:
        return results_path.stat().st_mtime
    except OSError:
        return None


def rebuild_from_results(config: Config) -> int:
    """Drop and rebuild the SQLite store from results.json. Returns case count."""
    results_path = config.results_json
    with open(results_path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)

    conn = connect(config.db_path)
    try:
        conn.executescript("DROP TABLE IF EXISTS cases; DROP TABLE IF EXISTS fts; DROP TABLE IF EXISTS meta;")
        _init_schema(conn)
        cases = doc.get("cases", [])
        for case in cases:
            _upsert_case(conn, case)
        _set_meta(conn, "schema_version", str(SCHEMA_VERSION))
        mtime = _results_mtime(results_path)
        if mtime is not None:
            _set_meta(conn, "results_mtime", repr(mtime))
        _set_meta(conn, "rebuilt_at", datetime.now(timezone.utc).isoformat())
        conn.commit()
        return len(cases)
    finally:
        conn.close()


def is_stale(config: Config) -> bool:
    """True if the DB is missing/empty or older than results.json."""
    db_path = config.db_path
    if not db_path.is_file():
        return True
    results_mtime = _results_mtime(config.results_json)
    if results_mtime is None:
        return False  # no canonical file to compare against; keep existing DB
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if not row:
            return True
        stored = _get_meta(conn, "results_mtime")
    finally:
        conn.close()
    if stored is None:
        return True
    try:
        return results_mtime > float(stored) + 1e-6
    except ValueError:
        return True


def ensure_fresh(config: Config) -> bool:
    """Rebuild the DB if stale. Returns True if a rebuild happened."""
    if is_stale(config):
        rebuild_from_results(config)
        return True
    return False


def export_results(config: Config) -> Path:
    """Write results.json from the current DB and re-sync the staleness token."""
    results_path = config.results_json
    conn = connect(config.db_path)
    try:
        rows = conn.execute("SELECT data FROM cases").fetchall()
        cases = [normalize_case(json.loads(r["data"])) for r in rows]
        cases.sort(key=lambda c: (c.get("box", ""), c.get("page_range") or [], c.get("case_id", "")))
        doc = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "archive_root": str(config.archive_root),
            "cases": cases,
        }
        results_path.parent.mkdir(parents=True, exist_ok=True)
        with open(results_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        # Re-sync so our own export is not flagged as "results.json newer than DB".
        mtime = _results_mtime(results_path)
        if mtime is not None:
            _set_meta(conn, "results_mtime", repr(mtime))
            conn.commit()
        return results_path
    finally:
        conn.close()


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
def list_cases(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
    box: Optional[str] = None,
) -> List[Dict[str, Any]]:
    sql = (
        "SELECT case_id, box, is_appeal, review_status, case_number, plaintiff, "
        "defendant, district, date_heard, page_count FROM cases"
    )
    clauses: List[str] = []
    params: List[Any] = []
    if status:
        clauses.append("review_status = ?")
        params.append(status)
    if box:
        clauses.append("box = ?")
        params.append(box)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY box, case_id"
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d["is_appeal"] = bool(d["is_appeal"])
        out.append(d)
    return out


def get_case(conn: sqlite3.Connection, case_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT data FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if not row:
        return None
    return normalize_case(json.loads(row["data"]))


def list_boxes(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute("SELECT DISTINCT box FROM cases ORDER BY box").fetchall()
    return [r["box"] for r in rows if r["box"]]


def status_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    counts = {s: 0 for s in REVIEW_STATUSES}
    for r in conn.execute("SELECT review_status, COUNT(*) c FROM cases GROUP BY review_status"):
        counts[r["review_status"]] = r["c"]
    return counts


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------
def update_case(conn: sqlite3.Connection, case_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Apply an edit payload to a case, setting ``edited`` slots (gemini kept).

    payload may contain:
      * ``fields``: {field_name: <edited str or null>}
      * ``pages``:  {filename: <edited transcript str or null>}
      * ``review_status``: one of REVIEW_STATUSES
      * ``notes``: str
    Returns the updated, normalized case (or None if not found).
    """
    case = get_case(conn, case_id)
    if case is None:
        return None

    for name, value in (payload.get("fields") or {}).items():
        if name in case["fields"]:
            case["fields"][name]["edited"] = value if value not in ("", None) else None

    page_edits = payload.get("pages") or {}
    if page_edits:
        for page in case["pages"]:
            if page["filename"] in page_edits:
                value = page_edits[page["filename"]]
                page["transcript"]["edited"] = value if value not in ("", None) else None

    if "review_status" in payload and payload["review_status"] in REVIEW_STATUSES:
        case["review_status"] = payload["review_status"]

    if "notes" in payload:
        case["notes"] = payload["notes"] or ""

    _upsert_case(conn, case)
    conn.commit()
    return normalize_case(case)


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------
def _build_match_query(raw: str) -> str:
    """Turn free user text into a safe FTS5 MATCH query (terms AND-ed, prefix)."""
    terms = [t for t in raw.replace('"', " ").split() if t]
    if not terms:
        return ""
    # Quote each term (handles punctuation) and allow prefix matching.
    return " AND ".join('"%s"*' % t for t in terms)


def search(
    conn: sqlite3.Connection,
    query: str,
    scope: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Ranked FTS search. ``scope`` restricts to a single field/transcript/notes.

    Returns hits: {case_id, box, review_status, field, snippet, case_number,
    plaintiff, defendant}.
    """
    match = _build_match_query(query)
    if not match:
        return []

    sql = (
        "SELECT f.case_id AS case_id, f.field AS field, "
        "snippet(fts, 2, '<mark>', '</mark>', '…', 12) AS snip, "
        "bm25(fts) AS rank, "
        "c.box AS box, c.review_status AS review_status, "
        "c.case_number AS case_number, c.plaintiff AS plaintiff, c.defendant AS defendant "
        "FROM fts f JOIN cases c ON c.case_id = f.case_id "
        "WHERE fts MATCH ?"
    )
    params: List[Any] = [match]
    if scope and scope in SEARCH_SCOPES:
        sql += " AND f.field = ?"
        params.append(scope)
    if status:
        sql += " AND c.review_status = ?"
        params.append(status)
    sql += " ORDER BY rank LIMIT ?"
    params.append(int(limit))

    out: List[Dict[str, Any]] = []
    for r in conn.execute(sql, params).fetchall():
        out.append(
            {
                "case_id": r["case_id"],
                "field": r["field"],
                "snippet": r["snip"],
                "box": r["box"],
                "review_status": r["review_status"],
                "case_number": r["case_number"],
                "plaintiff": r["plaintiff"],
                "defendant": r["defendant"],
            }
        )
    return out
