"""Fixed-window rate limits in the shared store (one INCR + EXPIRE per check)."""

from __future__ import annotations

import time
from dataclasses import dataclass

from sluice.store import Store


@dataclass
class LimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_s: int


async def hit(store: Store, name: str, limit: int, window_s: int) -> LimitResult:
    now = int(time.time())
    bucket = now // window_s
    key = f"sluice:rl:{name}:{window_s}:{bucket}"
    count, _ = await store.pipeline([["INCR", key], ["EXPIRE", key, window_s + 5]])
    count = int(count or 0)
    reset = (bucket + 1) * window_s - now
    return LimitResult(count <= limit, limit, max(0, limit - count), reset)
