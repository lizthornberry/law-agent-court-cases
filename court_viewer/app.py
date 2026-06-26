"""FastAPI application for the court_viewer.

Endpoints (all JSON unless noted):

  GET  /                         -> the single-page frontend (HTML)
  GET  /api/meta                 -> fields, statuses, search scopes, counts, boxes
  GET  /api/cases                -> list cases (filters: status, box)
  GET  /api/cases/{case_id}      -> one full case (tri-value fields + pages)
  PUT  /api/cases/{case_id}      -> save edits (sets edited slots) + export results.json
  GET  /api/search               -> ranked FTS search (q, scope/scopes, status, limit)
  POST /api/export               -> force export results.json from the DB
  POST /api/rebuild              -> force rebuild the DB from results.json
  GET  /api/image                -> full-size image bytes ({archive_root}/{box}/{filename})
  GET  /api/thumb                -> on-demand cached Pillow thumbnail

The DB is rebuilt from results.json on startup when stale, and re-checked before
each request so an externally-synced results.json is picked up automatically.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps

from . import db
from .config import Config, load_config
from .viewer_schema import FIELD_NAMES, REVIEW_STATUSES

PKG_DIR = Path(__file__).resolve().parent


def create_app(config: Optional[Config] = None) -> FastAPI:
    cfg = config or load_config()
    app = FastAPI(title="Court Transcription Viewer", version="1.0.0")
    app.state.config = cfg

    static_dir = PKG_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ----------------------------------------------------------------
    # DB freshness: rebuild from results.json when it is newer / missing.
    # ----------------------------------------------------------------
    @app.on_event("startup")
    def _startup() -> None:
        if cfg.results_json.is_file():
            db.ensure_fresh(cfg)

    def _conn():
        # Pick up an externally-updated results.json before serving.
        if cfg.results_json.is_file():
            db.ensure_fresh(cfg)
        return db.connect(cfg.db_path)

    # ----------------------------------------------------------------
    # frontend
    # ----------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        index_path = PKG_DIR / "templates" / "index.html"
        return HTMLResponse(index_path.read_text(encoding="utf-8"))

    # ----------------------------------------------------------------
    # metadata
    # ----------------------------------------------------------------
    @app.get("/api/meta")
    def meta() -> Dict[str, Any]:
        conn = _conn()
        try:
            return {
                "fields": FIELD_NAMES,
                "statuses": REVIEW_STATUSES,
                "search_scopes": db.SEARCH_SCOPES,
                "status_counts": db.status_counts(conn),
                "boxes": db.list_boxes(conn),
                "archive_root": str(cfg.archive_root),
                "results_json": str(cfg.results_json),
            }
        finally:
            conn.close()

    # ----------------------------------------------------------------
    # cases
    # ----------------------------------------------------------------
    @app.get("/api/cases")
    def cases(status: Optional[str] = None, box: Optional[str] = None) -> Dict[str, Any]:
        conn = _conn()
        try:
            return {"cases": db.list_cases(conn, status=status, box=box)}
        finally:
            conn.close()

    @app.get("/api/cases/{case_id}")
    def case_detail(case_id: str) -> Dict[str, Any]:
        conn = _conn()
        try:
            case = db.get_case(conn, case_id)
        finally:
            conn.close()
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        return case

    @app.put("/api/cases/{case_id}")
    def save_case(case_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        conn = _conn()
        try:
            updated = db.update_case(conn, case_id, payload)
        finally:
            conn.close()
        if updated is None:
            raise HTTPException(status_code=404, detail="case not found")
        # Export the canonical results.json after every save.
        db.export_results(cfg)
        return {"ok": True, "case": updated}

    # ----------------------------------------------------------------
    # search
    # ----------------------------------------------------------------
    @app.get("/api/search")
    def search(
        q: str = Query(..., min_length=1),
        scope: Optional[str] = None,
        scopes: Optional[List[str]] = Query(None),
        status: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        conn = _conn()
        try:
            hits = db.search(conn, q, scope=scope, scopes=scopes, status=status, limit=limit)
        finally:
            conn.close()
        active = scopes or ([p.strip() for p in scope.split(",") if p.strip()] if scope else None)
        return {"query": q, "scope": scope, "scopes": active, "status": status, "count": len(hits), "hits": hits}

    # ----------------------------------------------------------------
    # sync
    # ----------------------------------------------------------------
    @app.post("/api/export")
    def export() -> Dict[str, Any]:
        path = db.export_results(cfg)
        return {"ok": True, "results_json": str(path)}

    @app.post("/api/rebuild")
    def rebuild() -> Dict[str, Any]:
        if not cfg.results_json.is_file():
            raise HTTPException(status_code=404, detail="results.json not found")
        n = db.rebuild_from_results(cfg)
        return {"ok": True, "cases": n}

    # ----------------------------------------------------------------
    # images
    # ----------------------------------------------------------------
    def _safe_image_path(box: str, filename: str) -> Path:
        root = cfg.archive_root.resolve()
        for part in (box, filename):
            if not part or part in (".", "..") or "/" in part or "\\" in part:
                raise HTTPException(status_code=400, detail="invalid path component")
        # Do not resolve() the candidate: batch-test archive_root uses symlinks to
        # the real image tree, and resolve() would follow them outside the root.
        candidate = root / box / filename
        try:
            candidate.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes archive root")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="image not found")
        return candidate

    @app.get("/api/image")
    def image(box: str, filename: str) -> FileResponse:
        path = _safe_image_path(box, filename)
        return FileResponse(str(path))

    @app.get("/api/thumb")
    def thumb(box: str, filename: str) -> Response:
        src = _safe_image_path(box, filename)
        thumbs_root = cfg.thumbnails_dir.resolve()
        cached = (thumbs_root / box / (filename + ".jpg")).resolve()
        # Ensure the cache target also stays within the thumbnails dir.
        try:
            cached.relative_to(thumbs_root)
        except ValueError:
            raise HTTPException(status_code=403, detail="bad thumbnail path")

        if cached.is_file() and cached.stat().st_mtime >= src.stat().st_mtime:
            return FileResponse(str(cached), media_type="image/jpeg")

        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                im = im.convert("RGB")
                im.thumbnail((cfg.thumb_max_dimension, cfg.thumb_max_dimension))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=cfg.thumb_jpeg_quality)
                data = buf.getvalue()
        except Exception as exc:  # noqa: BLE001 - surface as 500 with context
            raise HTTPException(status_code=500, detail=f"thumbnail error: {exc}")

        cached.parent.mkdir(parents=True, exist_ok=True)
        try:
            cached.write_bytes(data)
        except OSError:
            pass  # serving still works even if the cache write fails
        return Response(content=data, media_type="image/jpeg")

    return app


# Module-level app for ``uvicorn court_viewer.app:app``.
app = create_app()
