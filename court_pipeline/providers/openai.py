"""OpenAI adapter (GPT-4o / GPT-4.1 vision)."""

from __future__ import annotations

import base64

from ..config import Config
from ..util import parse_json_lenient
from .base import LLMRequest, LLMResult, Provider


class OpenAIProvider(Provider):
    supports_batch = False

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ImportError("openai is not installed. Run: pip install openai") from exc
        api_key = cfg.api_key_for("openai")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        self.client = OpenAI(api_key=api_key)

    def generate(self, req: LLMRequest) -> LLMResult:
        content = []
        for img in req.images:
            b64 = base64.b64encode(img).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                }
            )
        content.append({"type": "text", "text": req.prompt})
        resp = self.client.chat.completions.create(
            model=req.model or self.model,
            temperature=0.0,
            max_tokens=req.max_output_tokens,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": content}],
        )
        text = resp.choices[0].message.content or ""
        try:
            return LLMResult(text=text, parsed=parse_json_lenient(text), key=req.key)
        except ValueError as exc:
            return LLMResult(text=text, parsed=None, error=str(exc), key=req.key)
