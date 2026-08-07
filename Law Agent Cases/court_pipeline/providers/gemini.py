"""Google Gemini adapter (default provider). Uses the google-genai SDK."""

from __future__ import annotations

import base64
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Config
from ..util import parse_json_lenient
from .base import LLMRequest, LLMResult, Provider

# Jobs at or below this size use the simpler inline batch path; larger jobs use
# the file-based JSONL path (the inline payload doesn't scale to thousands).
_INLINE_BATCH_MAX = 0  # 0 = always prefer the file-based path
_BATCH_POLL_SECONDS = 20
# All states the job will not move out of. EXPIRED / PARTIALLY_SUCCEEDED are
# terminal too -- omitting them (as the original code did) makes polling hang
# forever. PARTIALLY_SUCCEEDED still produces a result file we can parse.
_TERMINAL_STATES = {
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
    "JOB_STATE_PARTIALLY_SUCCEEDED",
}
# States that yield a (possibly partial) result file worth downloading/parsing.
_RESULT_STATES = {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}
# Stop polling after this long and surface the job name/state so a long-running
# job can be checked later instead of blocking indefinitely.
_BATCH_POLL_TIMEOUT_SECONDS = 120 * 60
# Gemini Files API rejects input JSONL above 2 GB; keep chunks safely below.
_BATCH_JSONL_MAX_BYTES = int(1.8 * 1024**3)
# Pro image batches often exceed the 30-min poll cap above ~500 requests.
_BATCH_MAX_REQUESTS = 400


class GeminiProvider(Provider):
    supports_batch = True

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        try:
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "google-genai is not installed. Run: pip install google-genai"
            ) from exc
        api_key = cfg.api_key_for("gemini")
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY (or GEMINI_API_KEY) is not set.")
        self._genai = genai
        self._types = types
        self.client = genai.Client(api_key=api_key)

    def _contents(self, req: LLMRequest):
        parts = []
        for img in req.images:
            parts.append(self._types.Part.from_bytes(data=img, mime_type="image/jpeg"))
        parts.append(req.prompt)
        return parts

    def _config(self, req: LLMRequest):
        return self._types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=req.max_output_tokens,
            temperature=0.0,
        )

    def generate(self, req: LLMRequest) -> LLMResult:
        resp = self.client.models.generate_content(
            model=req.model or self.model,
            contents=self._contents(req),
            config=self._config(req),
        )
        text = getattr(resp, "text", "") or ""
        try:
            parsed = parse_json_lenient(text)
            return LLMResult(text=text, parsed=parsed, key=req.key)
        except ValueError as exc:
            return LLMResult(text=text, parsed=None, error=str(exc), key=req.key)

    # -- batch ----------------------------------------------------------
    #
    # IMPORTANT (needs real-account verification): the file-based JSONL batch
    # path below is written to the documented google-genai batch API but could
    # NOT be exercised end-to-end here (no spare Pro quota / batch results to
    # download). The request-building, upload, polling and result-parsing are
    # guarded with try/except so any failure raises RuntimeError and stops the
    # run (no automatic live fallback). Verify against a real account
    # (a tiny <=2 flash-model job is enough) before relying on it for a large
    # run. The result file format is assumed to be JSONL lines of either
    #   {"key": "...", "response": {<GenerateContentResponse>}}  or
    #   {"key": "...", "error": {...}} / {"key": "...", "status": {...}}.
    def generate_batch(
        self, requests: List[LLMRequest], model: Optional[str] = None,
        pass_name: Optional[str] = None,
    ) -> List[LLMResult]:
        """Submit ONE async batch (all requests must share a model) and poll.

        Callers group requests by model before calling (batch is per-model).
        Returns results aligned to inputs by `key`. Raises RuntimeError if
        submission or polling fails (batch failures are fatal).
        """
        if not requests:
            return []
        model = model or requests[0].model or self.model
        pn = pass_name or requests[0].pass_name or "transcribe"

        if _INLINE_BATCH_MAX and len(requests) <= _INLINE_BATCH_MAX:
            try:
                return self._generate_batch_inline(requests, model)
            except Exception as exc:  # pragma: no cover - depends on SDK/account
                raise RuntimeError(f"Gemini inline batch failed: {exc}") from exc

        try:
            chunks = self._chunk_requests_by_jsonl_size(requests)
            if len(chunks) == 1:
                return self._generate_batch_file(chunks[0], model, pass_name=pn)
            out: List[LLMResult] = []
            for i, chunk in enumerate(chunks, 1):
                print(
                    f"[batch] submitting chunk {i}/{len(chunks)} "
                    f"({len(chunk)} requests, model={model})"
                )
                out.extend(self._generate_batch_file(chunk, model, pass_name=pn))
            return out
        except RuntimeError:
            raise
        except Exception as exc:  # pragma: no cover - depends on SDK/account
            raise RuntimeError(f"Gemini file batch failed: {exc}") from exc

    def resume_pending_batches(
        self, pass_name: str, poll_seconds: int | None = None,
    ) -> tuple[List[LLMResult], set[str]]:
        """Poll saved batch jobs. Returns (finished results, keys still running)."""
        from ..batch_pending import load_pending, remove_pending

        deadline = time.monotonic() + (poll_seconds or _BATCH_POLL_TIMEOUT_SECONDS)
        results: List[LLMResult] = []
        still_running: set[str] = set()
        for job in load_pending(self.cfg):
            if job.get("pass_name") != pass_name:
                continue
            name = job["job_name"]
            keys = job.get("keys") or []
            stubs = [LLMRequest(prompt="", key=k) for k in keys]
            print(f"[batch] resuming pending job {name} ({len(keys)} keys) ...", flush=True)
            state = ""
            timed_out = False
            while True:
                batch_job = self.client.batches.get(name=name)
                state = str(getattr(batch_job.state, "name", batch_job.state))
                if state in _TERMINAL_STATES:
                    break
                if time.monotonic() >= deadline:
                    print(
                        f"[batch] job {name} still {state}; will retry on next run",
                        flush=True,
                    )
                    still_running.update(keys)
                    timed_out = True
                    break
                time.sleep(_BATCH_POLL_SECONDS)
            if timed_out:
                continue
            print(f"[batch] pending job {name} reached {state}", flush=True)
            if state not in _RESULT_STATES:
                remove_pending(self.cfg, name)
                continue
            results.extend(self._download_batch_job(batch_job, stubs))
            remove_pending(self.cfg, name)
        return results, still_running

    def _download_batch_job(self, job: Any, requests: List[LLMRequest]) -> List[LLMResult]:
        dest = getattr(job, "dest", None)
        result_file = getattr(dest, "file_name", None) or getattr(dest, "file", None)
        if not result_file:
            return self._results_from_inlined(requests, dest)
        raw = self.client.files.download(file=result_file)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        by_key = self._parse_result_jsonl(raw)
        return self._align(requests, by_key)

    @staticmethod
    def _estimate_jsonl_line_bytes(req: LLMRequest) -> int:
        """Rough serialized JSONL line size (base64 images dominate)."""
        b64 = sum((len(img) + 2) // 3 * 4 for img in req.images)
        return b64 + len(req.prompt) + 512

    def _chunk_requests_by_jsonl_size(
        self, requests: List[LLMRequest]
    ) -> List[List[LLMRequest]]:
        chunks: List[List[LLMRequest]] = []
        current: List[LLMRequest] = []
        current_size = 0
        for req in requests:
            line_size = self._estimate_jsonl_line_bytes(req)
            over_bytes = current and current_size + line_size > _BATCH_JSONL_MAX_BYTES
            over_count = current and len(current) >= _BATCH_MAX_REQUESTS
            if over_bytes or over_count:
                chunks.append(current)
                current = []
                current_size = 0
            current.append(req)
            current_size += line_size
        if current:
            chunks.append(current)
        return chunks

    # -- file-based JSONL batch (scales to large jobs) ------------------
    def _request_body(self, req: LLMRequest) -> Dict[str, Any]:
        """REST-shaped GenerateContentRequest body with inline base64 image(s)."""
        parts: List[Dict[str, Any]] = []
        for img in req.images:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(img).decode("ascii"),
                    }
                }
            )
        parts.append({"text": req.prompt})
        return {
            "contents": [{"role": "user", "parts": parts}],
            "generation_config": {
                "response_mime_type": "application/json",
                "max_output_tokens": req.max_output_tokens,
                "temperature": 0.0,
            },
        }

    def _generate_batch_file(
        self, requests: List[LLMRequest], model: str, *, pass_name: str = "transcribe",
    ) -> List[LLMResult]:
        # 1) Write a JSONL file: one {"key", "request"} object per line.
        tmp_dir = Path(tempfile.mkdtemp(prefix="court_batch_"))
        jsonl_path = tmp_dir / "requests.jsonl"
        n_req = len(requests)
        print(
            f"[batch] writing JSONL for {n_req} request(s) (model={model}) ...",
            flush=True,
        )
        with open(jsonl_path, "w", encoding="utf-8") as fh:
            for i, req in enumerate(requests, 1):
                key = req.key or f"req-{i - 1}"
                fh.write(json.dumps({"key": key, "request": self._request_body(req)}) + "\n")
                if i == 1 or i % 100 == 0 or i == n_req:
                    print(f"[batch] JSONL {i}/{n_req} lines written", flush=True)

        # 2) Upload the JSONL via the files API.
        jsonl_bytes = jsonl_path.stat().st_size
        print(
            f"[batch] uploading JSONL ({jsonl_bytes / 1024 / 1024:.0f} MB) ...",
            flush=True,
        )
        try:
            uploaded = self.client.files.upload(
                file=str(jsonl_path),
                config=self._types.UploadFileConfig(
                    display_name="court_pipeline_batch", mime_type="jsonl"
                ),
            )
        except KeyError as exc:
            if exc.args == ("file",):
                raise RuntimeError(
                    f"Files API upload rejected input JSONL "
                    f"({jsonl_bytes / 1024 / 1024:.0f} MB; Gemini limit is 2048 MB)"
                ) from exc
            raise

        # 3) Create the batch job with the uploaded file as src.
        job = self.client.batches.create(
            model=model,
            src=uploaded.name,
            config={"display_name": "court_pipeline_pages"},
        )

        # 4) Poll to completion.
        name = job.name
        print(f"[batch] submitted job {name} ({len(requests)} requests); polling ...", flush=True)
        deadline = time.monotonic() + _BATCH_POLL_TIMEOUT_SECONDS
        while True:
            job = self.client.batches.get(name=name)
            state = str(getattr(job.state, "name", job.state))
            if state in _TERMINAL_STATES:
                break
            if time.monotonic() >= deadline:
                from ..batch_pending import save_pending

                save_pending(
                    self.cfg,
                    job_name=name,
                    model=model,
                    keys=[r.key for r in requests if r.key],
                    pass_name=pass_name,
                )
                raise RuntimeError(
                    f"batch job {name} still {state} after "
                    f"{_BATCH_POLL_TIMEOUT_SECONDS}s; saved to batch_pending.json — "
                    f"re-run transcribe to poll and continue"
                )
            time.sleep(_BATCH_POLL_SECONDS)
        print(f"[batch] job {name} reached terminal state {state}", flush=True)
        if state not in _RESULT_STATES:
            raise RuntimeError(f"batch job {name} ended in state {state}")

        # 5) Download + parse the result file, mapping responses back by key.
        return self._download_batch_job(job, requests)

    def _parse_result_jsonl(self, raw: str) -> Dict[str, LLMResult]:
        by_key: Dict[str, LLMResult] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = obj.get("key")
            if obj.get("error") or obj.get("status"):
                by_key[key] = LLMResult(
                    text="", parsed=None, error=str(obj.get("error") or obj.get("status")),
                    key=key,
                )
                continue
            text = self._text_from_response(obj.get("response") or {})
            err = None
            try:
                parsed = parse_json_lenient(text) if text else None
            except ValueError as exc:
                parsed, err = None, str(exc)
            by_key[key] = LLMResult(text=text, parsed=parsed, error=err, key=key)
        return by_key

    @staticmethod
    def _text_from_response(response: Dict[str, Any]) -> str:
        """Pull concatenated text out of a REST GenerateContentResponse dict."""
        try:
            candidates = response.get("candidates") or []
            if not candidates:
                return ""
            parts = (candidates[0].get("content") or {}).get("parts") or []
            return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        except Exception:  # pragma: no cover - defensive
            return ""

    def _align(
        self, requests: List[LLMRequest], by_key: Dict[str, LLMResult]
    ) -> List[LLMResult]:
        out: List[LLMResult] = []
        for i, req in enumerate(requests):
            key = req.key or f"req-{i}"
            out.append(
                by_key.get(key)
                or LLMResult(text="", parsed=None, error="no batch result for key", key=key)
            )
        return out

    def _results_from_inlined(self, requests: List[LLMRequest], dest: Any) -> List[LLMResult]:
        responses = getattr(dest, "inlined_responses", None) or []
        results: List[LLMResult] = []
        for req, item in zip(requests, responses):
            text, err = "", None
            try:
                text = getattr(item.response, "text", "") or ""
            except Exception as exc:  # pragma: no cover
                err = str(exc)
            try:
                parsed = parse_json_lenient(text) if text else None
            except ValueError as exc:
                parsed, err = None, str(exc)
            results.append(LLMResult(text=text, parsed=parsed, error=err, key=req.key))
        return results

    # -- inline batch (small jobs) -------------------------------------
    def _generate_batch_inline(self, requests: List[LLMRequest], model: str) -> List[LLMResult]:
        inlined = [
            {"contents": self._contents(r), "config": self._config(r)} for r in requests
        ]
        job = self.client.batches.create(
            model=model, src=inlined, config={"display_name": "court_pipeline_pages"}
        )
        name = job.name
        while True:
            job = self.client.batches.get(name=name)
            state = str(getattr(job.state, "name", job.state))
            if state in _TERMINAL_STATES:
                break
            time.sleep(_BATCH_POLL_SECONDS)
        return self._results_from_inlined(requests, getattr(job, "dest", None))
