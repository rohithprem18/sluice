"""HTTP surface.

POST /v1/chat/completions   OpenAI-compatible (JSON or SSE); Bearer key auth
GET  /v1/models             virtual + concrete models
POST /api/playground        public demo: NDJSON trace + tokens, rate limited per IP
POST /api/redact            PII preview, no model call
GET  /api/stats             dashboard metrics
GET  /api/health            provider / backend status
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from sluice import __version__, limits, metrics
from sluice.cache import CALIBRATED_THRESHOLDS, MemoryVectorIndex, ResponseCache, UpstashVectorIndex
from sluice.config import Settings, get_settings
from sluice.embeddings import GeminiEmbedder, HashEmbedder
from sluice.gateway import Context, Gateway, GatewayError, Options
from sluice.guard import PromptGuard
from sluice.pii import Redactor
from sluice.providers import OpenAIClient, catalog, providers
from sluice.router import VIRTUAL, Router
from sluice.store import MemoryStore, Store, UpstashStore


class Services:
    """Wires the gateway from settings. Keyword overrides exist for tests."""

    def __init__(
        self,
        s: Settings,
        *,
        store: Store | None = None,
        index=None,
        embedder=None,
        client: OpenAIClient | None = None,
        guard: PromptGuard | None = None,
    ):
        self.settings = s
        self.store: Store = store or (UpstashStore(s.redis_url, s.redis_token) if s.has_redis else MemoryStore())
        index = index or (UpstashVectorIndex(s.vector_url, s.vector_token) if s.has_vector else MemoryVectorIndex())
        embedder = embedder or (GeminiEmbedder(s.gemini_api_key) if s.gemini_api_key else HashEmbedder())
        self.deployments = catalog(s)
        self.router = Router(self.deployments, self.store, s.breaker_failures, s.breaker_cooldown_s)
        self.cache = ResponseCache(self.store, index, embedder, s.cache_threshold, s.cache_ttl_s)
        self.guard = guard or PromptGuard(s.groq_api_key, s.guard_threshold)
        self.gateway = Gateway(s, self.router, client or OpenAIClient(s.provider_timeout_s), self.cache, self.guard)
        self.backends = {
            "store": "upstash-redis" if s.has_redis else "memory",
            "vector": "upstash-vector" if s.has_vector else "memory",
            "embeddings": "gemini-embedding-2 (+2 fallbacks)" if s.gemini_api_key else "hash (offline)",
        }


_services: Services | None = None


def services() -> Services:
    global _services
    if _services is None:
        _services = Services(get_settings())
    return _services


def set_services(svc: Services | None) -> None:
    """Test hook."""
    global _services
    _services = svc


app = FastAPI(title="Sluice", version=__version__, description="OpenAI-compatible gateway over free LLM providers")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-sluice-request-id", "x-sluice-served-by", "x-sluice-cache"],
)


def _error(status: int, message: str, code: str, headers: dict | None = None) -> JSONResponse:
    kind = "invalid_request_error" if status < 500 else "api_error"
    return JSONResponse(
        {"error": {"message": message, "type": kind, "code": code}}, status_code=status, headers=headers
    )


def _flag(request: Request, name: str, default: bool = True) -> bool:
    v = request.headers.get(f"x-sluice-{name}")
    return default if v is None else v.strip().lower() not in ("0", "off", "false", "no")


def _outage(raw: str | None) -> frozenset[str]:
    return frozenset(x.strip().lower() for x in (raw or "").split(",") if x.strip())


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def _auth(request: Request, svc: Services) -> str | None:
    keys = svc.settings.api_keys
    if not keys:
        return "local"  # no keys configured: local development
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return keys.get(token)


# ---------------------------------------------------------------------- OpenAI-compatible
@app.get("/v1/models")
async def list_models():
    svc = services()
    now = int(time.time())
    data = [{"id": k, "object": "model", "created": now, "owned_by": "sluice"} for k in VIRTUAL if "/" in k]
    data += [
        {"id": d.id, "object": "model", "created": now, "owned_by": d.provider.name, "tier": d.tier}
        for d in svc.deployments
    ]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    svc = services()
    client = _auth(request, svc)
    if client is None:
        return _error(401, "invalid or missing API key", "invalid_api_key")
    rl = await limits.hit(svc.store, f"key:{client}", svc.settings.key_rpm, 60)
    rl_headers = {
        "x-ratelimit-limit-requests": str(rl.limit),
        "x-ratelimit-remaining-requests": str(rl.remaining),
        "x-ratelimit-reset-requests": f"{rl.reset_s}s",
    }
    if not rl.allowed:
        return _error(429, f"rate limit of {rl.limit} requests/min exceeded", "rate_limited", rl_headers)
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return _error(400, "body must be JSON", "invalid_json")
    ctx = Context(
        client=client,
        options=Options(
            cache=_flag(request, "cache"),
            redact=_flag(request, "redact"),
            guard=_flag(request, "guard"),
            simulate_outage=_outage(request.headers.get("x-sluice-simulate-outage")),
        ),
    )
    if not payload.get("stream"):
        try:
            data = await svc.gateway.complete(payload, ctx)
        except GatewayError as e:
            return _error(e.status, str(e), e.code, rl_headers)
        meta = data.get("sluice", {})
        return JSONResponse(
            data,
            headers={
                **rl_headers,
                "x-sluice-request-id": ctx.request_id,
                "x-sluice-served-by": str(meta.get("served_by")),
                "x-sluice-cache": str(meta.get("cache") or "miss"),
            },
        )
    events = svc.gateway.stream(payload, ctx)
    first = await _first_output(events)
    if first and first[-1]["type"] == "error":
        e = first[-1]
        return _error(e["status"], e["message"], e["code"], rl_headers)
    return StreamingResponse(
        _sse(first, events, ctx.request_id),
        media_type="text/event-stream",
        headers={**rl_headers, "x-sluice-request-id": ctx.request_id, "cache-control": "no-cache"},
    )


async def _first_output(events: AsyncIterator[dict]) -> list[dict]:
    """Buffer events up to the first token (or error) so errors can still set the HTTP status."""
    buf = []
    async for ev in events:
        buf.append(ev)
        if ev["type"] in ("delta", "chunk", "done", "error"):
            break
    return buf


async def _sse(buffered: list[dict], events: AsyncIterator[dict], rid: str) -> AsyncIterator[bytes]:
    created = int(time.time())

    def chunk(model: str, delta: dict | None = None, finish=None, extra: dict | None = None) -> bytes:
        body = {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if extra:
            body.update(extra)
        return f"data: {json.dumps(body)}\n\n".encode()

    async def all_events():
        for ev in buffered:
            yield ev
        async for ev in events:
            yield ev

    model = "sluice"
    sent_role = False
    async for ev in all_events():
        t = ev["type"]
        if t == "delta":
            model = ev.get("model") or model
            delta = {"content": ev["text"]}
            if not sent_role:
                delta["role"] = "assistant"
                sent_role = True
            yield chunk(model, delta)
        elif t == "chunk":
            c = ev["chunk"]
            c.update({"id": rid, "model": ev.get("model") or model})
            yield f"data: {json.dumps(c)}\n\n".encode()
        elif t == "done":
            yield chunk(model, {}, "stop")
            yield chunk(model, None, extra={"usage": ev.get("usage"), "sluice": ev["meta"]})
        elif t == "error":
            yield f"data: {json.dumps({'error': {'message': ev['message'], 'code': ev['code']}})}\n\n".encode()
    yield b"data: [DONE]\n\n"


# ---------------------------------------------------------------------- console API
class PlaygroundRequest(BaseModel):
    messages: list[dict] = Field(min_length=1, max_length=20)
    model: str = "sluice/auto"
    temperature: float | None = 0.2
    max_tokens: int = Field(default=600, le=1500)
    cache: bool = True
    redact: bool = True
    guard: bool = True
    simulate_outage: list[str] = Field(default_factory=list, max_length=4)


@app.post("/api/playground")
async def playground(body: PlaygroundRequest, request: Request):
    svc = services()
    s = svc.settings
    per_ip = await limits.hit(svc.store, f"pg:{_ip(request)}", s.playground_per_hour, 3600)
    if not per_ip.allowed:
        return _error(
            429,
            f"playground limit is {s.playground_per_hour} requests/hour; try again in "
            f"{per_ip.reset_s // 60 + 1} min",
            "rate_limited",
        )
    total = await limits.hit(svc.store, "pg:all", s.playground_per_day_total, 86400)
    if not total.allowed:
        return _error(429, "the public playground's daily budget is used up; it resets at 00:00 UTC", "rate_limited")
    payload = {
        "model": body.model,
        "messages": body.messages,
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
    }
    opts = Options(
        cache=body.cache, redact=body.redact, guard=body.guard, simulate_outage=_outage(",".join(body.simulate_outage))
    )
    ctx = Context(client="playground", options=opts)

    async def ndjson():
        async for ev in svc.gateway.stream(payload, ctx):
            if ev["type"] == "chunk":
                continue
            yield (json.dumps(ev) + "\n").encode()

    return StreamingResponse(
        ndjson(),
        media_type="application/x-ndjson",
        headers={"cache-control": "no-cache", "x-remaining-hour": str(per_ip.remaining)},
    )


class RedactRequest(BaseModel):
    text: str = Field(max_length=10_000)


@app.post("/api/redact")
async def redact_preview(body: RedactRequest):
    masked, red = Redactor().redact(body.text)
    return {
        "masked": masked,
        "found": red.counts,
        "entities": [{"placeholder": k, "kind": k[1:].rsplit("_", 1)[0]} for k in red.mapping],
    }


@app.get("/api/stats")
async def stats():
    svc = services()
    snap = await metrics.snapshot(svc.store, [d.id for d in svc.deployments])
    health = await svc.router.health()
    for row in snap["deployments"]:
        h = health.get(row["deployment"])
        dep = next(d for d in svc.deployments if d.id == row["deployment"])
        row.update(
            {
                "provider": dep.provider.label,
                "model": dep.model,
                "tier": dep.tier,
                "ewma_ms": round(h.latency_ms, 1) if h else None,
                "state": "open" if h and h.is_open else ("degraded" if h and h.failures else "healthy"),
            }
        )
    return snap


@app.get("/api/health")
async def health():
    svc = services()
    return {
        "status": "ok",
        "version": __version__,
        "backends": svc.backends,
        "providers": [
            {"name": p.name, "label": p.label, "enabled": p.enabled} for p in providers(svc.settings).values()
        ],
        "deployments": len(svc.deployments),
        "guard": svc.guard.enabled and svc.settings.guard_enabled,
        "cache_threshold": svc.settings.cache_threshold or CALIBRATED_THRESHOLDS,
        "limits": {"playground_per_hour": svc.settings.playground_per_hour, "key_rpm": svc.settings.key_rpm},
    }


# Local convenience: serve the built console when it exists (Vercel serves it natively).
_static = Path(os.getenv("SLUICE_STATIC_DIR", Path(__file__).resolve().parent.parent / "web" / "dist"))
if (_static / "index.html").exists() and os.getenv("VERCEL") is None:
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_static, html=True), name="console")
