"""Settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _keys(raw: str) -> dict[str, str]:
    """SLUICE_API_KEYS="quarry:sk-...,triage:sk-..." -> {secret: client name}."""
    out: dict[str, str] = {}
    for item in filter(None, (x.strip() for x in raw.split(","))):
        name, _, secret = item.partition(":")
        if secret:
            out[secret.strip()] = name.strip()
    return out


@dataclass(frozen=True)
class Settings:
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""))
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    openrouter_api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    cerebras_api_key: str = field(default_factory=lambda: os.getenv("CEREBRAS_API_KEY", ""))

    redis_url: str = field(default_factory=lambda: os.getenv("UPSTASH_REDIS_REST_URL", ""))
    redis_token: str = field(default_factory=lambda: os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))
    vector_url: str = field(default_factory=lambda: os.getenv("UPSTASH_VECTOR_REST_URL", ""))
    vector_token: str = field(default_factory=lambda: os.getenv("UPSTASH_VECTOR_REST_TOKEN", ""))

    api_keys: dict[str, str] = field(default_factory=lambda: _keys(os.getenv("SLUICE_API_KEYS", "")))

    # semantic cache
    # None = per-embedding-model thresholds calibrated on QQP (see cache.CALIBRATED_THRESHOLDS)
    cache_threshold: float | None = field(default_factory=lambda: _float("SLUICE_CACHE_THRESHOLD", 0) or None)
    cache_ttl_s: int = field(default_factory=lambda: _int("SLUICE_CACHE_TTL_S", 7 * 24 * 3600))
    cache_max_temperature: float = field(default_factory=lambda: _float("SLUICE_CACHE_MAX_TEMPERATURE", 0.5))

    # limits
    key_rpm: int = field(default_factory=lambda: _int("SLUICE_KEY_RPM", 60))
    playground_per_hour: int = field(default_factory=lambda: _int("SLUICE_PLAYGROUND_PER_HOUR", 20))
    playground_per_day_total: int = field(default_factory=lambda: _int("SLUICE_PLAYGROUND_PER_DAY", 400))
    max_output_tokens: int = field(default_factory=lambda: _int("SLUICE_MAX_OUTPUT_TOKENS", 2048))

    # routing / resilience
    provider_timeout_s: float = field(default_factory=lambda: _float("SLUICE_PROVIDER_TIMEOUT_S", 30))
    breaker_failures: int = field(default_factory=lambda: _int("SLUICE_BREAKER_FAILURES", 3))
    breaker_cooldown_s: int = field(default_factory=lambda: _int("SLUICE_BREAKER_COOLDOWN_S", 60))

    guard_enabled: bool = field(default_factory=lambda: os.getenv("SLUICE_GUARD", "1") == "1")
    guard_threshold: float = field(default_factory=lambda: _float("SLUICE_GUARD_THRESHOLD", 0.9))

    @property
    def has_redis(self) -> bool:
        return bool(self.redis_url and self.redis_token)

    @property
    def has_vector(self) -> bool:
        return bool(self.vector_url and self.vector_token)


def get_settings() -> Settings:
    return Settings()
