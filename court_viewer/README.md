# court_viewer

A single-user web **viewer/editor** for the archival court-transcription project.
It lets you correct transcription errors and extracted field data, set a review
status, add notes, and search (full-text and field-restricted) across the
corpus.

`court_viewer/` is fully self-contained and treats everything under
`court_pipeline/` as **read-only** upstream input. It never edits pipeline code.

---

## Quick start

```bash
cd "/Users/liz/Library/CloudStorage/OneDrive-JohnsHopkins/Cursor Projects"
source .venv/bin/activate
pip install -r court_viewer/requirements.txt

# 1) Build the canonical results.json from the pipeline outputs:
python -m court_viewer.build_results            # -> court_pipeline/output/results.json

#    ...or generate a small offline SAMPLE instead (no API quota used):
python -m court_viewer.sample_data --output court_pipeline/output/results.json

# 2) Run the server (rebuilds the SQLite DB from results.json on startup if stale):
python -m uvicorn court_viewer.app:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

Configuration lives in `court_viewer/config.yaml`. You can point the app at an
alternate config with the `COURT_VIEWER_CONFIG` environment variable.

---

## Data model & sync semantics

### `results.json` — the canonical, portable source of truth

`results.json` lives in the OneDrive tree (default
`court_pipeline/output/results.json`, configurable) and is the artifact that
travels between machines. Shape:

```jsonc
{
  "schema_version": 1,
  "generated_at": "<iso8601>",
  "archive_root": "<abs path>",
  "cases": [ /* see below */ ]
}
```

Each case:

```jsonc
{
  "case_id": "...", "box": "...", "is_appeal": false, "page_range": [28, 30],
  "review_status": "unreviewed",          // unreviewed | in progress | verified | flagged
  "notes": "",
  "source_images": ["IMG_6402.jpg", ...],
  "fields": {                              // every extracted field, tri-value
    "case_number":   {"gemini": "25/91", "claude": null, "edited": null},
    "district": {...}, "magistrate": {...}, "plaintiff": {...}, "defendant": {...},
    "claim": {...}, "date_heard": {...}, "date_heard_iso": {...},
    "appearance_for_plaintiff": {...}, "interpreter": {...}, "plea_verbatim": {...},
    "verdict": {...}, "full_transcript": {...}
  },
  "pages": [
    {"filename": "IMG_6402.jpg", "order": 28, "page_type": "cover_regular",
     "transcript": {"gemini": "...", "claude": null, "edited": null}}
  ],
  "field_confidence": {"plaintiff": "high", ...},
  "provenance": {"provider": "gemini", "model": "...", "processed_at": "..."}
}
```

**Tri-value semantics.** Every field value and every page transcript is a
`{gemini, claude, edited}` object:

* The **EFFECTIVE / current** value = `edited` if non-null, else `gemini`.
* `claude` is the **ALTERNATE** (reserved for a future Gemini-vs-Claude compare
  view). It is preserved on every regenerate/save but is **never indexed for
  search**. In v1 it is usually `null`.
* A user edit sets the `edited` slot only; `gemini` is left intact for
  provenance/compare.

### SQLite (`viewer.db`) — local, disposable working store

SQLite is a fast local mirror, **rebuilt from `results.json`** whenever
`results.json` is newer (or the DB is missing). Staleness is decided by
comparing the live mtime of `results.json` against a `results_mtime` token
stored in the DB's `meta` table:

* **Startup** and **before every request** the app calls `db.ensure_fresh()`,
  which rebuilds when stale. This means an externally-synced `results.json`
  (e.g. pulled from another machine via OneDrive) is picked up automatically.
* **On SAVE** the edit is written to SQLite, then `results.json` is **exported
  from SQLite**, and the staleness token is re-synced to the freshly written
  file's mtime (so the app's own export is not mistaken for an external change).

So the flow is: `results.json` → (rebuild) → SQLite → (edit) → SQLite →
(export) → `results.json`.

---

## Components

| File | Purpose |
|------|---------|
| `config.yaml` / `config.py` | Paths (results_json, db, archive_root, thumbnails, pipeline dirs) + thumbnail + server settings. Paths resolve relative to `config.yaml`. |
| `viewer_schema.py` | `results.json` schema constants + helpers (`FIELD_NAMES`, `REVIEW_STATUSES`, tri-value, `effective()`, `normalize_case()`). |
| `build_results.py` | Builds/refreshes `results.json` from `output/cases/*.json` + `data/pages/<box>/<file>.json`. Idempotent and **edit-preserving** (see below). |
| `db.py` | SQLite + FTS5: rebuild from results.json, staleness check, export, search, reads/writes. |
| `app.py` | FastAPI app: REST endpoints + image/thumbnail serving + serves the frontend. |
| `templates/index.html`, `static/app.js`, `static/style.css` | Lightweight vanilla-JS frontend. |
| `sample_data.py` | Generates a small offline SAMPLE `results.json` (no API calls). |

### `build_results.py` — preserving user work on regenerate

When `results.json` already exists, the builder merges by `case_id`:

* **Preserved** from the existing file: `edited` slots (fields + page
  transcripts), `claude` alternates, `notes`, `review_status`.
* **Refreshed** from the pipeline: `gemini` slots, `page_type`, `order`,
  `page_range`, `source_images`, `is_appeal`, `field_confidence`, `provenance`.
* Cases that exist only in `results.json` (e.g. synthesized samples) are kept so
  user work is never dropped.

This makes re-running the pipeline + rebuilding safe: corrections survive.

---

## HTTP API

| Method & path | Description |
|---|---|
| `GET /` | The single-page frontend. |
| `GET /api/meta` | fields, statuses, search scopes, per-status counts, boxes, paths. |
| `GET /api/cases?status=&box=` | List cases (cached effective scalars for the list view). |
| `GET /api/cases/{case_id}` | One full case (tri-value fields + pages). |
| `PUT /api/cases/{case_id}` | Save edits (sets `edited` slots), then export `results.json`. Body: `{fields:{name:val|null}, pages:{filename:val|null}, review_status, notes}`. |
| `GET /api/search?q=&scope=&status=&limit=` | Ranked FTS search. `scope` restricts to one field / `transcript` / `notes`. |
| `POST /api/export` | Force export `results.json` from the DB. |
| `POST /api/rebuild` | Force rebuild the DB from `results.json`. |
| `GET /api/image?box=&filename=` | Full-size image bytes from `{archive_root}/{box}/{filename}`. |
| `GET /api/thumb?box=&filename=` | On-demand Pillow thumbnail, cached to the thumbnails dir. |

### Search (FTS5)

A single FTS5 table `fts(case_id UNINDEXED, field UNINDEXED, content)` holds one
row per searchable unit:

* every **effective** field value (`field` = the field name),
* every **effective** page transcript (`field` = `transcript`),
* the case **notes** (`field` = `notes`).

`claude` alternates are deliberately **not inserted**, so they are never
searchable. Full-text search matches `content`; field-restricted search adds
`WHERE field = ?`; the status filter joins `cases` and adds
`WHERE review_status = ?`. Results are ranked by `bm25()` and returned with
`snippet()` highlights and the owning `case_id`. The FTS rows for a case are
rebuilt on every save, so fresh edits appear in search immediately.

### Editing UX / dirty guard

* Edits are staged in the form and only committed on an explicit **Save**.
* A **dirty indicator** appears in the top bar when there are unsaved changes.
* An **unsaved-changes guard** (Save / Discard / Cancel) fires when switching to
  another case, on in-app navigation (search/clear/status change), and on
  browser tab close (`beforeunload`).
* Editable: every field's effective value, every page transcript, the case-level
  `full_transcript`, `review_status`, and `notes`. Saving sets the `edited` slot
  (or clears it back to `null` when the value equals the `gemini` baseline),
  leaving `gemini` untouched.

### Images & path-traversal protection

Both image endpoints resolve `{archive_root}/{box}/{filename}` and then verify
the resolved path is still inside `archive_root` (`Path.resolve()` +
`relative_to`), returning **403** if it escapes and **404** if missing. The
thumbnail endpoint additionally confirms the cache target stays within the
thumbnails dir, applies EXIF transpose, downscales with Pillow, and caches a
JPEG (re-generated if the source is newer).

---

## Sample data (offline, no API quota)

`python -m court_viewer.sample_data` synthesizes four representative cases that
match the schema exactly and reference **real** image filenames under the real
box `1-NKE 2-1-1-11`, so the image/thumbnail endpoints work against actual
files. The sample exercises: gemini-only fields, a pre-existing `edited`
override, a populated `claude` alternate (preserved but not searched), all four
review statuses, notes, and field confidence. It writes to an isolated path and
never touches `court_pipeline/data` or `court_pipeline/output`.

---

## Future: Gemini-vs-Claude compare view

The schema already carries a `claude` slot on every field and page transcript.
To add the compare view later:

1. Populate `claude` slots (a second provider pass writing into `results.json`,
   merged by `build_results.py`, which already preserves `claude`).
2. Add a `GET /api/cases/{id}/compare` endpoint (or extend the detail payload)
   surfacing `gemini` vs `claude` vs `edited` per field/page.
3. In the frontend detail view, render a side-by-side diff with "accept gemini"
   / "accept claude" buttons that write the chosen text into `edited`.

No schema migration is required — only `claude` population and UI wiring.
