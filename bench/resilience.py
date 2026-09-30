"""Measure gateway overhead, cache-hit latency and outage survival against a live Sluice.

    SLUICE_URL=https://... SLUICE_KEY=sk-... python bench/resilience.py --n 12

Scenarios (each with distinct prompts so the cache cannot help unless intended):
  baseline       cache off; overhead = gateway time - provider time
  cache_hit      the same prompt repeated; served from cache
  outage_groq    Groq treated as down for the request
  outage_2       Groq and Gemini both down; only OpenRouter left
Timings come from the gateway's own trace (server side), so client network
distance does not distort them.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

import httpx

TOPICS = [
    "a mutex",
    "a B-tree",
    "TCP slow start",
    "a bloom filter",
    "CAP theorem",
    "a hash ring",
    "MVCC",
    "a write-ahead log",
    "backpressure",
    "idempotency",
    "a vector clock",
    "a skip list",
    "rate limiting",
    "consistent hashing",
    "a CRDT",
    "two-phase commit",
    "gossip protocols",
    "a LSM tree",
    "RAFT",
    "sharding",
]


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def call(c: httpx.Client, url, key, prompt, headers=None):
    body = {
        "model": "sluice/fast",
        "temperature": 0,
        "max_tokens": 60,
        "messages": [{"role": "user", "content": prompt}],
    }
    t = time.perf_counter()
    r = c.post(f"{url}/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}", **(headers or {})})
    client_ms = (time.perf_counter() - t) * 1000
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "client_ms": client_ms}
    m = r.json()["sluice"]
    ok_attempts = [a for a in m["attempts"] if a["ok"]]
    upstream = ok_attempts[-1]["ms"] if ok_attempts else 0.0
    return {
        "ok": True,
        "status": 200,
        "client_ms": client_ms,
        "gateway_ms": m["latency_ms"],
        "upstream_ms": upstream,
        "served_by": m["served_by"],
        "cache": m["cache"],
        "attempts": len(m["attempts"]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    args = ap.parse_args()
    url, key = os.environ["SLUICE_URL"].rstrip("/"), os.environ["SLUICE_KEY"]
    run = int(time.time())
    out: dict = {"url": url, "n": args.n, "scenarios": {}}
    with httpx.Client(timeout=90) as c:

        def scenario(name, prompts, headers):
            rows = []
            for p in prompts:
                rows.append(call(c, url, key, p, headers))
                time.sleep(1.2)  # stay inside free-tier per-minute limits
            ok = [r for r in rows if r["ok"]]
            summary = {
                "requests": len(rows),
                "succeeded": len(ok),
                "gateway_p50_ms": pct([r["gateway_ms"] for r in ok], 0.5),
                "gateway_p95_ms": pct([r["gateway_ms"] for r in ok], 0.95),
                "overhead_p50_ms": pct([r["gateway_ms"] - r["upstream_ms"] for r in ok if not r["cache"]], 0.5),
                "served_by": dict(
                    sorted({s: sum(1 for r in ok if r["served_by"] == s) for s in {r["served_by"] for r in ok}}.items())
                ),
                "avg_attempts": round(statistics.mean(r["attempts"] for r in ok), 2) if ok else None,
            }
            out["scenarios"][name] = summary
            print(name, json.dumps(summary))

        n = args.n
        scenario(
            "baseline",
            [f"[{run}-b{i}] Define {TOPICS[i % 20]} in one sentence." for i in range(n)],
            {"x-sluice-cache": "off"},
        )
        warm = f"[{run}-c] Define a semaphore in one sentence."
        call(c, url, key, warm)
        scenario("cache_hit", [warm] * n, {})
        scenario(
            "outage_groq",
            [f"[{run}-g{i}] Define {TOPICS[i % 20]} in one sentence." for i in range(n)],
            {"x-sluice-cache": "off", "x-sluice-simulate-outage": "groq"},
        )
        scenario(
            "outage_groq_gemini",
            [f"[{run}-o{i}] Define {TOPICS[i % 20]} in one sentence." for i in range(n)],
            {"x-sluice-cache": "off", "x-sluice-simulate-outage": "groq,gemini"},
        )
    path = Path(__file__).resolve().parent / "results" / "resilience.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
