"""Stable provenance helpers for LLM-generated records."""

from __future__ import annotations

import hashlib
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict


def prompt_hash(prompt_template: str) -> str:
    """SHA-256 of the exact prompt template used by a pipeline stage."""
    return hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def code_provenance() -> Dict[str, Any]:
    """Return HEAD SHA and whether the pipeline source directory was dirty.

    The commit SHA remains a valid Git object even for a dirty worktree; the
    separate flag prevents that SHA from falsely implying exact reproducibility.
    Empty/None values mean the source tree was not inside a Git checkout.
    """
    repo_hint = Path(__file__).resolve().parent
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo_hint), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        status = subprocess.run(
            [
                "git", "-C", str(repo_hint), "status", "--porcelain",
                "--untracked-files=normal", "--", ".",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        return {"git_commit": commit, "git_dirty": bool(status.strip())}
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": "", "git_dirty": None}


def stage_provenance(prompt_template: str) -> Dict[str, Any]:
    """Provenance common to one stage invocation."""
    return {
        "prompt_hash": prompt_hash(prompt_template),
        **code_provenance(),
    }
