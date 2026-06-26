"""Provider interface shared by all vision-LLM adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import Config


@dataclass
class LLMRequest:
    """A single generation request (one prompt + zero or more JPEG images)."""

    prompt: str
    images: List[bytes] = field(default_factory=list)
    max_output_tokens: int = 8192
    # Arbitrary id the caller uses to match batch responses back to inputs.
    key: Optional[str] = None
    # Optional per-request model override. When set, the provider uses this
    # model instead of its default (cfg.model). Lets one provider instance call
    # different models per request (e.g. a cheap classify model vs a routed
    # transcribe model). Backward compatible: None means "use self.model".
    model: Optional[str] = None
    pass_name: Optional[str] = None


@dataclass
class LLMResult:
    text: str
    parsed: Any = None
    error: Optional[str] = None
    key: Optional[str] = None


class Provider:
    """Base class. Subclasses implement `generate` (and optionally batch)."""

    supports_batch: bool = False

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model = cfg.model

    def generate(self, req: LLMRequest) -> LLMResult:  # pragma: no cover - interface
        raise NotImplementedError

    def generate_batch(
        self, requests: List[LLMRequest], model: Optional[str] = None
    ) -> List[LLMResult]:
        """Async/batch path. Default: not supported.

        `model` lets the caller pin one batch to a single model (batch APIs are
        typically per-model); callers group requests by model before calling.
        """
        raise NotImplementedError(
            f"Provider {self.cfg.provider!r} does not support batch mode; use run.mode: live"
        )
