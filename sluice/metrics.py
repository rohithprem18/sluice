"""Request telemetry: per-minute counters, latency samples and a recent-request log."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field

from sluice.store import Store, hash_to_dict

LOG_KEY = "sluice:log"
TOTALS_KEY = "sluice:totals"
LOG_SIZE = 200
LAT_SAMPLES = 300
BUCKET_TTL_S = 2 * 24 * 3600


@dataclass
class Event:
    id: str
    ts: float
    client: str
    model_requested: str
    tier: str
    served_by: str | None  # deployment id, or None on cache hit / failure
    status: int
    latency_ms: float
    upstream_ms: float | None
    cache: str | None  # "exact" | "semantic" | None
    similarity: float | None
    attempts: list[dict] = field(default_factory=list)
    pii: dict[str, int] = field(default_factory=dict)
    guard_score: float | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    stream: bool = False
    error: str | None = None


def minute(ts: float | None = None) -> int:
    return int((ts or time.time()) // 60)


async def record(store: Store, ev: Event) -> None:
    b = f"sluice:m:{minute(ev.ts)}"
    ok = 200 <= ev.status < 300
    cmds: list[list] = [
        ["LPUSH", LOG_KEY, json.dumps(asdict(ev))],
        ["LTRIM", LOG_KEY, 0, LOG_SIZE - 1],
        ["HINCRBY", b, "req", 1],
        ["HINCRBY", b, "err", 0 if ok else 1],
        ["HINCRBYFLOAT", b, "lat_sum", round(ev.latency_ms, 1)],
        ["HINCRBY", b, "tok_in", ev.tokens_in],
        ["HINCRBY", b, "tok_out", ev.tokens_out],
        ["EXPIRE", b, BUCKET_TTL_S],
        ["HINCRBY", TOTALS_KEY, "req", 1],
        ["HINCRBY", TOTALS_KEY, "err", 0 if ok else 1],
        ["HINCRBY", TOTALS_KEY, "tok_in", ev.tokens_in],
        ["HINCRBY", TOTALS_KEY, "tok_out", ev.tokens_out],
    ]
    if ev.cache:
        cmds += [
            ["HINCRBY", b, f"hit_{ev.cache}", 1],
            ["HINCRBY", TOTALS_KEY, f"hit_{ev.cache}", 1],
            ["HINCRBY", TOTALS_KEY, "tok_saved", ev.tokens_out],
        ]
    if len(ev.attempts) > 1 and ok:
        cmds += [["HINCRBY", b, "fallback", 1], ["HINCRBY", TOTALS_KEY, "fallback", 1]]
    if ev.pii:
        n = sum(ev.pii.values())
        cmds += [["HINCRBY", b, "pii", n], ["HINCRBY", TOTALS_KEY, "pii", n]]
    if ev.guard_score is not None and ev.status == 403:
        cmds += [["HINCRBY", TOTALS_KEY, "blocked", 1]]
    for a in ev.attempts:
        dep = a["deployment"]
        cmds += [
            ["HINCRBY", TOTALS_KEY, f"dep|{dep}|req", 1],
            ["HINCRBY", TOTALS_KEY, f"dep|{dep}|err", 0 if a["ok"] else 1],
        ]
        if a["ok"]:
            cmds += [
                ["LPUSH", f"sluice:lat:{dep}", round(a["ms"], 1)],
                ["LTRIM", f"sluice:lat:{dep}", 0, LAT_SAMPLES - 1],
            ]
    await store.pipeline(cmds)


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


async def snapshot(store: Store, deployments: list[str], minutes: int = 60) -> dict:
    now = minute()
    cmds: list[list] = [["HGETALL", f"sluice:m:{m}"] for m in range(now - minutes + 1, now + 1)]
    cmds += [["HGETALL", TOTALS_KEY], ["LRANGE", LOG_KEY, 0, 49]]
    cmds += [["LRANGE", f"sluice:lat:{d}", 0, LAT_SAMPLES - 1] for d in deployments]
    res = await store.pipeline(cmds)
    buckets = [hash_to_dict(r) for r in res[:minutes]]
    totals = {k: float(v) for k, v in hash_to_dict(res[minutes]).items()}
    log = [json.loads(x) for x in (res[minutes + 1] or [])]
    lat = {d: [float(x) for x in (res[minutes + 2 + i] or [])] for i, d in enumerate(deployments)}

    series = []
    for i, b in enumerate(buckets):
        req = int(float(b.get("req", 0)))
        series.append(
            {
                "minute": (now - minutes + 1 + i) * 60,
                "requests": req,
                "errors": int(float(b.get("err", 0))),
                "cache_hits": int(float(b.get("hit_exact", 0))) + int(float(b.get("hit_semantic", 0))),
                "avg_latency_ms": round(float(b.get("lat_sum", 0)) / req, 1) if req else None,
            }
        )
    req = totals.get("req", 0)
    hits = totals.get("hit_exact", 0) + totals.get("hit_semantic", 0)
    per_dep = []
    for d in deployments:
        n = totals.get(f"dep|{d}|req", 0)
        per_dep.append(
            {
                "deployment": d,
                "requests": int(n),
                "errors": int(totals.get(f"dep|{d}|err", 0)),
                "p50_ms": percentile(lat[d], 0.5),
                "p95_ms": percentile(lat[d], 0.95),
            }
        )
    return {
        "totals": {
            "requests": int(req),
            "errors": int(totals.get("err", 0)),
            "cache_hits": int(hits),
            "cache_hit_rate": round(hits / req, 4) if req else 0.0,
            "semantic_hits": int(totals.get("hit_semantic", 0)),
            "exact_hits": int(totals.get("hit_exact", 0)),
            "fallbacks": int(totals.get("fallback", 0)),
            "pii_redacted": int(totals.get("pii", 0)),
            "blocked": int(totals.get("blocked", 0)),
            "tokens_in": int(totals.get("tok_in", 0)),
            "tokens_out": int(totals.get("tok_out", 0)),
            "tokens_saved": int(totals.get("tok_saved", 0)),
        },
        "series": series,
        "deployments": per_dep,
        "recent": log,
    }
