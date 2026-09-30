"""Model routing with health-aware ordering and circuit breakers.

Virtual models:
  sluice/auto   picks a tier from the request (default)
  sluice/smart  large models first
  sluice/fast   small, low-latency models first
A concrete model id ("groq/openai/gpt-oss-120b" or just "openai/gpt-oss-120b")
is tried first, with the rest of its tier as fallbacks.

Health lives in the shared store so every serverless instance agrees: an EWMA
of latency per deployment, a consecutive-failure count, and an open-until
timestamp. A deployment that fails `breaker_failures` times in a row is skipped
for `breaker_cooldown_s`, then gets one trial request (half-open).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from sluice.providers import Deployment
from sluice.store import Store, hash_to_dict

HEALTH_KEY = "sluice:health"
VIRTUAL = {"sluice/auto": "auto", "sluice/smart": "smart", "sluice/fast": "fast", "auto": "auto"}
DEFAULT_LATENCY_MS = 1500.0
EWMA_ALPHA = 0.3

_SMART_HINTS = re.compile(
    r"```|\b(step[- ]by[- ]step|prove|derive|analy[sz]e|compare|trade-?offs?|design|architecture|debug|refactor|"
    r"sql|algorithm|explain why|reason)\b",
    re.I,
)


def classify_tier(payload: dict) -> tuple[str, str]:
    """Cheap, deterministic tier choice for sluice/auto. Returns (tier, reason)."""
    if payload.get("tools") or payload.get("response_format"):
        return "smart", "tools or structured output requested"
    text = " ".join(
        m.get("content", "") if isinstance(m.get("content"), str) else "" for m in payload.get("messages", [])
    )
    if len(text) > 1500:
        return "smart", f"long prompt ({len(text)} chars)"
    m = _SMART_HINTS.search(text)
    if m:
        return "smart", f"reasoning cue: '{m.group(0).strip()}'"
    return "fast", "short, simple prompt"


@dataclass
class Health:
    latency_ms: float = DEFAULT_LATENCY_MS
    failures: int = 0
    open_until: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.open_until > time.time()


@dataclass
class Plan:
    tier: str
    reason: str
    candidates: list[Deployment]
    skipped: list[str]
    health: dict[str, Health]


class Router:
    def __init__(self, deployments: list[Deployment], store: Store, failures: int = 3, cooldown_s: int = 60):
        self.deployments = deployments
        self.store = store
        self.failures = failures
        self.cooldown_s = cooldown_s

    async def health(self) -> dict[str, Health]:
        raw = hash_to_dict(await self.store.cmd("HGETALL", HEALTH_KEY))
        out: dict[str, Health] = {}
        for d in self.deployments:
            out[d.id] = Health(
                latency_ms=float(raw.get(f"{d.id}|lat", DEFAULT_LATENCY_MS)),
                failures=int(float(raw.get(f"{d.id}|fail", 0))),
                open_until=float(raw.get(f"{d.id}|open", 0)),
            )
        return out

    def resolve(self, model: str | None) -> tuple[str, Deployment | None]:
        model = (model or "sluice/auto").strip()
        if model in VIRTUAL:
            return VIRTUAL[model], None
        for d in self.deployments:
            if model in (d.id, d.model):
                return d.tier, d
        return "unknown", None

    async def plan(self, payload: dict) -> Plan:
        tier, pinned = self.resolve(payload.get("model"))
        if tier == "unknown":
            raise LookupError(f"unknown model '{payload.get('model')}'")
        reason = "requested"
        if tier == "auto":
            tier, reason = classify_tier(payload)
        elif pinned:
            reason = f"pinned to {pinned.id}"
        health = await self.health()
        # requests with tools only go to deployments that return structured tool calls
        capable = [d for d in self.deployments if d.tools] if payload.get("tools") else list(self.deployments)
        pool = [d for d in capable if d.tier == tier] or capable or list(self.deployments)
        ranked = sorted(pool, key=lambda d: (health[d.id].is_open, health[d.id].latency_ms, pool.index(d)))
        if pinned:
            ranked = [pinned] + [d for d in ranked if d.id != pinned.id]
        # the other tier as a last resort, so a request never dies while anything is up
        ranked += [d for d in sorted(capable, key=lambda d: health[d.id].latency_ms) if d not in ranked]
        live = [d for d in ranked if not health[d.id].is_open]
        skipped = [d.id for d in ranked if health[d.id].is_open]
        return Plan(tier, reason, live or ranked, skipped, health)

    async def record(self, dep: Deployment, ok: bool, latency_ms: float, prior: Health | None = None) -> None:
        """Update health. `prior` (from the plan) saves a read round trip."""
        h = prior or (await self.health())[dep.id]
        if ok:
            lat = (
                latency_ms
                if h.latency_ms == DEFAULT_LATENCY_MS
                else EWMA_ALPHA * latency_ms + (1 - EWMA_ALPHA) * h.latency_ms
            )
            await self.store.cmd(
                "HSET", HEALTH_KEY, f"{dep.id}|lat", f"{lat:.1f}", f"{dep.id}|fail", 0, f"{dep.id}|open", 0
            )
            return
        fails = h.failures + 1
        opened = time.time() + self.cooldown_s if fails >= self.failures else h.open_until
        await self.store.cmd("HSET", HEALTH_KEY, f"{dep.id}|fail", fails, f"{dep.id}|open", f"{opened:.0f}")
