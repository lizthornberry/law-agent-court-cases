"""Configuration loading: reads config.yaml and resolves paths + secrets."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

import yaml
from dotenv import load_dotenv

PKG_DIR = Path(__file__).resolve().parent


class Config:
    """Thin wrapper around the parsed config.yaml with resolved paths."""

    def __init__(self, data: Dict[str, Any], config_path: Path):
        self._data = data
        self.config_path = config_path
        self.base_dir = config_path.parent

    # -- generic access -------------------------------------------------
    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    # -- frequently used values ----------------------------------------
    @property
    def provider(self) -> str:
        return self._data.get("provider", "gemini")

    @property
    def model(self) -> str:
        return self.get("models", self.provider, default=self.provider)

    # -- two-pass stage models & routing -------------------------------
    @property
    def classify_model(self) -> str:
        """Model for Pass A (classify). Falls back to the default model."""
        return self.get("stages", "classify", "model", default=self.model)

    @property
    def transcribe_default_model(self) -> str:
        """Default model for Pass B (transcribe) when no type override matches."""
        return self.get("stages", "transcribe", "default_model", default=self.model)

    @property
    def transcribe_type_models(self) -> Dict[str, str]:
        """Per-page_type model overrides for the transcribe pass."""
        return dict(self.get("stages", "transcribe", "type_models", default={}) or {})

    @property
    def transcribe_skip_types(self) -> List[str]:
        """Page types that are NOT transcribed (no API call, verbatim_text="")."""
        return list(
            self.get("stages", "transcribe", "skip_types", default=["blank", "box_photo"])
            or []
        )

    def transcribe_model_for(self, page_type: str) -> str:
        """Resolve which model transcribes a given page_type (type override else default)."""
        return self.transcribe_type_models.get(page_type, self.transcribe_default_model)

    # -- per-stage batch/live mode -------------------------------------
    def stage_mode(self, stage: str) -> str:
        """Resolve ``live`` or ``batch`` for a stage.

        ``stages.<stage>.mode`` overrides the global ``run.mode`` when set.
        """
        return self.get("stages", stage, "mode", default=self.get("run", "mode", default="live"))

    def stage_use_batch(self, stage: str, use_batch: bool | None = None) -> bool:
        """Whether a stage should use async batch mode.

        An explicit ``use_batch`` (e.g. from ``--batch`` on the CLI) overrides
        per-stage and global config.
        """
        if use_batch is not None:
            return use_batch
        return self.stage_mode(stage) == "batch"

    @property
    def classify_mode(self) -> str:
        return self.stage_mode("classify")

    @property
    def transcribe_mode(self) -> str:
        return self.stage_mode("transcribe")

    # -- per-stage PROVIDER selection ----------------------------------
    # Each stage may declare its own provider (gemini/anthropic/openai/mock) in
    # addition to its model. When a provider key is absent we fall back to the
    # global ``provider`` so existing configs keep their current behavior.
    @property
    def classify_provider(self) -> str:
        """Provider for Pass A (classify). Falls back to the global provider."""
        return self.get("stages", "classify", "provider", default=self.provider)

    @property
    def transcribe_default_provider(self) -> str:
        """Default provider for Pass B (transcribe) when no type override matches."""
        return self.get("stages", "transcribe", "default_provider", default=self.provider)

    @property
    def transcribe_type_providers(self) -> Dict[str, str]:
        """Per-page_type provider overrides for the transcribe pass."""
        return dict(self.get("stages", "transcribe", "type_providers", default={}) or {})

    def transcribe_provider_for(self, page_type: str) -> str:
        """Resolve which provider transcribes a given page_type (override else default)."""
        return self.transcribe_type_providers.get(page_type, self.transcribe_default_provider)

    @property
    def consolidate_provider(self) -> str:
        """Provider for the per-case consolidation (``cases``) stage."""
        return self.get("stages", "cases", "provider", default=self.provider)

    @property
    def consolidate_model(self) -> str:
        """Model for the per-case consolidation (``cases``) stage. Falls back to default model."""
        return self.get("stages", "cases", "model", default=self.model)

    def resolve(self, rel: str) -> Path:
        p = Path(rel).expanduser()
        if not p.is_absolute():
            p = (self.base_dir / p).resolve()
        return p

    @property
    def images_root(self) -> Path:
        return self.resolve(self.get("paths", "images_root", default="../Law Agent Civil Cases"))

    @property
    def data_dir(self) -> Path:
        return self.resolve(self.get("paths", "data_dir", default="data"))

    @property
    def output_dir(self) -> Path:
        return self.resolve(self.get("paths", "output_dir", default="output"))

    @property
    def pages_dir(self) -> Path:
        return self.data_dir / "pages"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.json"

    @property
    def cases_index_path(self) -> Path:
        return self.data_dir / "cases.json"

    @property
    def cases_out_dir(self) -> Path:
        return self.output_dir / "cases"

    # -- pricing --------------------------------------------------------
    def price_for_model(self, model: str) -> Dict[str, float]:
        """USD per 1M tokens for a model.

        Looks up `pricing.models.<model>` first (preferred, since the two-pass
        flow uses several models), then falls back to the provider-level
        `pricing.<provider>` block, then to zeros.
        """
        by_model = self.get("pricing", "models", model, default=None)
        if isinstance(by_model, dict):
            return {
                "input": float(by_model.get("input", 0.0)),
                "output": float(by_model.get("output", 0.0)),
            }
        by_provider = self.get("pricing", self.provider, default={}) or {}
        return {
            "input": float(by_provider.get("input", 0.0)),
            "output": float(by_provider.get("output", 0.0)),
        }

    # -- secrets --------------------------------------------------------
    def api_key_for(self, provider: str) -> str:
        """Resolve the API key for a NAMED provider (per-stage providers need this)."""
        if provider == "gemini":
            return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
        if provider == "anthropic":
            return os.environ.get("ANTHROPIC_API_KEY", "")
        if provider == "openai":
            return os.environ.get("OPENAI_API_KEY", "")
        return ""  # mock

    def api_key(self) -> str:
        return self.api_key_for(self.provider)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.pages_dir, self.output_dir, self.cases_out_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_config(config_path: str | os.PathLike | None = None) -> Config:
    """Load config.yaml (defaults to the one next to this package) and .env."""
    path = Path(config_path).resolve() if config_path else (PKG_DIR / "config.yaml")
    # Load .env from the package dir (and current dir as a fallback).
    load_dotenv(PKG_DIR / ".env")
    load_dotenv()
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Config(data, path)
