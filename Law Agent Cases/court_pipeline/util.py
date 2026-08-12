"""Small shared helpers: JSON parsing, natural sort, timestamps, hashing."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, List

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}

_NUM_RE = re.compile(r"(\d+)")


def natural_key(name: str) -> List[Any]:
    """Sort key so IMG_2 < IMG_10 (digit runs compared numerically)."""
    return [int(tok) if tok.isdigit() else tok.lower() for tok in _NUM_RE.split(name)]


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha1_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _extract_first_json(s: str) -> Any:
    """Recover the first JSON object/array from `s`, tolerating common model slips.

    Walks the text with a string-aware bracket counter starting at the first
    ``{``/``[``. This handles two malformations the legacy outermost-block
    fallback got wrong:
      * trailing junk after a valid object (e.g. a DOUBLED closing brace
        ``{...}\\n}`` -- the scan stops at the first balanced close and ignores
        the rest, instead of swallowing the stray brace or grabbing an inner
        array like ``["English"]``);
      * a TRUNCATED object missing its final close brace(s) -- the scan appends
        the outstanding closers (and a closing quote if it ended mid-string) and
        retries.
    Returns the parsed value, or ``None`` if nothing parseable was found.
    """
    candidates = [i for i in (s.find("{"), s.find("[")) if i != -1]
    if not candidates:
        return None
    start = min(candidates)
    stack: List[str] = []
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:
                try:
                    return json.loads(s[start : i + 1])
                except json.JSONDecodeError:
                    return None
    # Reached the end with brackets still open: likely truncated. Close it.
    if stack:
        candidate = s[start:]
        if in_str:
            candidate += '"'
        candidate += "".join(reversed(stack))
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None
    return None


def parse_json_lenient(text: str) -> Any:
    """Parse JSON from a model response, tolerating code fences / stray prose."""
    if text is None:
        raise ValueError("empty response")
    s = text.strip()
    # Strip ```json ... ``` fences.
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # String-aware recovery of the first balanced (or truncated) object/array.
    recovered = _extract_first_json(s)
    if recovered is not None:
        return recovered
    # Last resort: the outermost {...} or [...] block (legacy behavior).
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = s.find(open_ch)
        end = s.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(s[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("could not parse JSON from model response")


class DailyQuotaExceeded(Exception):
    """Raised when the provider reports the per-day request quota is exhausted."""


def is_daily_quota_error(msg: str | None) -> bool:
    if not msg:
        return False
    m = msg.lower()
    if "per_day" in m or "per day" in m or "requests_per_day" in m:
        return True
    if ("429" in m or "resource_exhausted" in m or "quota" in m) and "day" in m:
        return True
    return False


def is_rate_limit_error(msg: str | None) -> bool:
    if not msg:
        return False
    m = msg.lower()
    return "429" in m or "resource_exhausted" in m or "rate limit" in m or "quota" in m


def safe_slug(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len] or "unknown"
