"""Persist in-flight Gemini batch jobs so a timed-out poll can resume later."""

from __future__ import annotations

from typing import Any, Dict, List

from .config import Config
from .util import read_json, write_json


def _path(cfg: Config) -> str:
    return str(cfg.data_dir / "batch_pending.json")


def load_pending(cfg: Config) -> List[Dict[str, Any]]:
    p = cfg.data_dir / "batch_pending.json"
    if not p.exists():
        return []
    try:
        data = read_json(p)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_pending(
    cfg: Config,
    *,
    job_name: str,
    model: str,
    keys: List[str],
    pass_name: str,
) -> None:
    cfg.ensure_dirs()
    pending = load_pending(cfg)
    pending = [j for j in pending if j.get("job_name") != job_name]
    pending.append(
        {
            "job_name": job_name,
            "model": model,
            "keys": keys,
            "pass_name": pass_name,
        }
    )
    write_json(cfg.data_dir / "batch_pending.json", pending)


def remove_pending(cfg: Config, job_name: str) -> None:
    pending = [j for j in load_pending(cfg) if j.get("job_name") != job_name]
    write_json(cfg.data_dir / "batch_pending.json", pending)


def pending_keys(cfg: Config, pass_name: str) -> set[str]:
    out: set[str] = set()
    for job in load_pending(cfg):
        if job.get("pass_name") == pass_name:
            out.update(job.get("keys") or [])
    return out
