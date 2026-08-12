"""Provider adapter layer (provider-agnostic vision-LLM access)."""

from __future__ import annotations

from ..config import Config
from .base import Provider


def _build_provider(cfg: Config, name: str) -> Provider:
    if name == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(cfg)
    if name == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(cfg)
    if name == "openai":
        from .openai import OpenAIProvider

        return OpenAIProvider(cfg)
    if name == "mock":
        from .mock import MockProvider

        return MockProvider(cfg)
    raise ValueError(f"Unknown provider: {name!r}")


def get_provider_named(cfg: Config, name: str) -> Provider:
    """Return a (memoized) provider instance by name.

    Builds gemini/anthropic/openai/mock on demand and caches the instances on
    the ``cfg`` object so a single run can mix providers per stage (e.g. a cheap
    Gemini flash classifier + a Claude Opus transcriber) without reconstructing
    SDK clients for every request.
    """
    cache = getattr(cfg, "_provider_cache", None)
    if cache is None:
        cache = {}
        setattr(cfg, "_provider_cache", cache)
    if name not in cache:
        cache[name] = _build_provider(cfg, name)
    return cache[name]


def get_provider(cfg: Config) -> Provider:
    """Backward-compatible single provider chosen by the global ``cfg.provider``."""
    return get_provider_named(cfg, cfg.provider)


__all__ = ["Provider", "get_provider", "get_provider_named"]
