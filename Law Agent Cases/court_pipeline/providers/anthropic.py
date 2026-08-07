"""Anthropic Claude adapter."""

from __future__ import annotations

import base64

from ..config import Config
from ..util import parse_json_lenient
from .base import LLMRequest, LLMResult, Provider


class AnthropicProvider(Provider):
    supports_batch = False

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        try:
            import anthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ImportError("anthropic is not installed. Run: pip install anthropic") from exc
        api_key = cfg.api_key_for("anthropic")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        self.client = anthropic.Anthropic(api_key=api_key)

    def generate(self, req: LLMRequest) -> LLMResult:
        content = []
        for img in req.images:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(img).decode("ascii"),
                    },
                }
            )
        content.append({"type": "text", "text": req.prompt})
        kwargs = dict(
            model=req.model or self.model,
            max_tokens=req.max_output_tokens,
            messages=[{"role": "user", "content": content}],
        )
        # We prefer temperature=0.0 for deterministic transcription, but newer
        # Claude models (e.g. Opus 4.x) REJECT the `temperature` param with a 400
        # ("temperature is deprecated for this model"). Send it, and transparently
        # retry once without it if the API rejects it.
        try:
            msg = self.client.messages.create(temperature=0.0, **kwargs)
        except Exception as exc:  # noqa: BLE001 - inspect message, then re-raise
            if "temperature" in str(exc).lower():
                msg = self.client.messages.create(**kwargs)
            else:
                raise
        text = "".join(
            block.text for block in msg.content if getattr(block, "type", "") == "text"
        )
        try:
            return LLMResult(text=text, parsed=parse_json_lenient(text), key=req.key)
        except ValueError as exc:
            return LLMResult(text=text, parsed=None, error=str(exc), key=req.key)
