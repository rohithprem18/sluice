"""The request pipeline.

    redact -> route -> (injection screen || cache lookup) -> providers (with fallback)
           -> restore PII -> cache store -> record

The injection screen runs concurrently with the cache lookup; a blocked prompt
is still rejected before anything is returned.

`stream()` yields typed events that the OpenAI-compatible endpoint turns into
SSE chunks and the playground turns into a live trace; `complete()` is the
non-streaming path and also carries tool calls through untouched.
"""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from sluice import metrics
from sluice.cache import ResponseCache
from sluice.config import Settings
from sluice.guard import PromptGuard
from sluice.pii import Redaction, Redactor, StreamRestorer, restore
from sluice.providers import Deployment, OpenAIClient, UpstreamError
from sluice.router import Health, Router

MAX_ATTEMPTS = 4
CLIENT_ERRORS = (400, 422)


class GatewayError(Exception):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass
class Options:
    cache: bool = True
    redact: bool = True
    guard: bool = True
    # providers to treat as down for this request only (demo / resilience tests);
    # simulated failures never touch the shared circuit breakers
    simulate_outage: frozenset[str] = frozenset()


class SimulatedOutage(UpstreamError):
    def __init__(self, dep: Deployment):
        super().__init__(503, f"{dep.id}: simulated outage", True)


@dataclass
class Context:
    client: str
    options: Options = field(default_factory=Options)
    request_id: str = field(default_factory=lambda: "req_" + uuid.uuid4().hex[:16])


@dataclass
class Prepared:
    payload: dict  # redacted, clamped payload sent upstream
    redaction: Redaction
    guard_score: float | None
    tier: str
    reason: str
    candidates: list[Deployment]
    skipped: list[str]
    cache_model: str
    cacheable: bool
    steps: list[dict]
    health: dict[str, Health] = field(default_factory=dict)


def _last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else " ".join(p.get("text", "") for p in c or [] if isinstance(p, dict))
    return ""


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


class Gateway:
    def __init__(
        self,
        settings: Settings,
        router: Router,
        client: OpenAIClient,
        cache: ResponseCache,
        guard: PromptGuard | None,
        redactor: Redactor | None = None,
    ):
        self.s, self.router, self.client, self.cache, self.guard = settings, router, client, cache, guard
        self.redactor = redactor or Redactor()

    # ------------------------------------------------------------------ prepare
    async def prepare(self, payload: dict, ctx: Context) -> Prepared:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise GatewayError(400, "'messages' must be a non-empty list", "invalid_request")
        steps: list[dict] = []
        body = copy.deepcopy(payload)
        limit = self.s.max_output_tokens
        for k in ("max_tokens", "max_completion_tokens"):
            if body.get(k):
                body[k] = min(int(body[k]), limit)

        redaction = Redaction()
        if ctx.options.redact:
            t = time.perf_counter()
            body["messages"], redaction = self.redactor.redact_messages(body["messages"])
            steps.append(
                {
                    "step": "redact",
                    "ms": round((time.perf_counter() - t) * 1000, 2),
                    "found": dict(redaction.counts),
                    # what the provider will see, shown only when something was masked
                    "preview": _last_user(body["messages"])[:400] if redaction.total else None,
                }
            )

        try:
            plan = await self.router.plan(body)
        except LookupError as e:
            raise GatewayError(404, str(e), "model_not_found") from e
        steps.append(
            {
                "step": "route",
                "tier": plan.tier,
                "reason": plan.reason,
                "order": [d.id for d in plan.candidates[:MAX_ATTEMPTS]],
                "skipped_open": plan.skipped,
            }
        )

        temp = body.get("temperature")
        cacheable = (
            ctx.options.cache
            and not body.get("tools")
            and (body.get("n") in (None, 1))
            and (temp is None or float(temp) <= self.s.cache_max_temperature)
        )
        _, pinned = self.router.resolve(body.get("model"))
        cache_model = pinned.id if pinned else f"tier:{plan.tier}"
        return Prepared(
            body,
            redaction,
            None,
            plan.tier,
            plan.reason,
            plan.candidates,
            plan.skipped,
            cache_model,
            cacheable,
            steps,
            plan.health,
        )

    async def _screen(self, p: Prepared, ctx: Context) -> None:
        if not (ctx.options.guard and self.s.guard_enabled and self.guard and self.guard.enabled):
            return
        t = time.perf_counter()
        score = await self.guard.score(_last_user(p.payload["messages"]))
        p.guard_score = score
        blocked = score is not None and score >= self.guard.threshold
        p.steps.append(
            {"step": "guard", "ms": round((time.perf_counter() - t) * 1000, 1), "score": score, "blocked": blocked}
        )
        if blocked:
            raise GatewayError(403, f"prompt blocked by injection screen (score {score:.3f})", "prompt_blocked")

    async def _screen_and_lookup(self, p: Prepared, ctx: Context):
        screen = asyncio.create_task(self._screen(p, ctx))
        try:
            found = await self._lookup(p)
        except BaseException:
            screen.cancel()
            raise
        if found[0] is not None and not screen.done():
            # A cached answer was generated for an earlier, already-screened prompt, and
            # this prompt never reaches a model, so the screen can be skipped.
            screen.cancel()
            p.steps.append({"step": "guard", "skipped": True, "score": None, "blocked": False})
            return found
        await screen  # raises GatewayError if the prompt is blocked
        return found

    def _attempt_order(self, p: Prepared, ctx: Context):
        """Candidates in order, capped at MAX_ATTEMPTS real calls (simulated outages are free)."""
        real = 0
        for dep in p.candidates:
            if real >= MAX_ATTEMPTS:
                return
            if dep.provider.name not in ctx.options.simulate_outage:
                real += 1
            yield dep

    async def _lookup(self, p: Prepared):
        if not p.cacheable:
            p.steps.append({"step": "cache", "result": "skipped"})
            return None, None
        t = time.perf_counter()
        try:
            hit, vec = await self.cache.lookup(p.cache_model, p.payload)
        except Exception as e:  # cache trouble must never fail a request
            p.steps.append({"step": "cache", "result": "error", "detail": e.__class__.__name__})
            return None, None
        p.steps.append(
            {
                "step": "cache",
                "ms": round((time.perf_counter() - t) * 1000, 1),
                "result": hit.kind if hit else "miss",
                "similarity": round(hit.similarity, 4) if hit else None,
                "matched": hit.matched_question if hit else None,
            }
        )
        return hit, vec

    async def _store(self, p: Prepared, content: str, served_by: str, vec) -> None:
        if not p.cacheable:
            return
        try:
            await self.cache.store_response(p.cache_model, p.payload, content, served_by, vec)
        except Exception:
            pass

    def _event(self, ctx: Context, p: Prepared | None, payload: dict, t0: float, **kw) -> metrics.Event:
        return metrics.Event(
            id=ctx.request_id,
            ts=time.time(),
            client=ctx.client,
            model_requested=str(payload.get("model") or "sluice/auto"),
            tier=p.tier if p else "",
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            pii=dict(p.redaction.counts) if p else {},
            guard_score=p.guard_score if p else None,
            **kw,
        )

    async def _record(self, ev: metrics.Event) -> None:
        try:
            await metrics.record(self.router.store, ev)
        except Exception:
            pass

    def _meta(self, ctx: Context, p: Prepared, t0: float, served_by, cache, attempts, similarity=None) -> dict:
        return {
            "request_id": ctx.request_id,
            "served_by": served_by,
            "tier": p.tier,
            "route_reason": p.reason,
            "cache": cache,
            "similarity": similarity,
            "attempts": attempts,
            "pii": dict(p.redaction.counts),
            "guard_score": p.guard_score,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "steps": p.steps,
        }

    # ------------------------------------------------------------------ non-streaming
    async def complete(self, payload: dict, ctx: Context) -> dict:
        t0 = time.perf_counter()
        try:
            p = await self.prepare(payload, ctx)
        except GatewayError as e:
            await self._record(
                self._event(
                    ctx,
                    None,
                    payload,
                    t0,
                    served_by=None,
                    status=e.status,
                    upstream_ms=None,
                    cache=None,
                    similarity=None,
                    error=str(e),
                )
            )
            raise
        try:
            hit, vec = await self._screen_and_lookup(p, ctx)
        except GatewayError as e:
            await self._record(
                self._event(
                    ctx,
                    p,
                    payload,
                    t0,
                    served_by=None,
                    status=e.status,
                    upstream_ms=None,
                    cache=None,
                    similarity=None,
                    error=str(e),
                )
            )
            raise
        if hit:
            content = restore(hit.content, p.redaction)
            meta = self._meta(ctx, p, t0, hit.source_model, hit.kind, [], round(hit.similarity, 4))
            await self._record(
                self._event(
                    ctx,
                    p,
                    payload,
                    t0,
                    served_by=None,
                    status=200,
                    upstream_ms=None,
                    cache=hit.kind,
                    similarity=hit.similarity,
                    tokens_out=_approx_tokens(content),
                )
            )
            return _completion(ctx.request_id, hit.source_model or p.cache_model, content, None, meta)

        attempts: list[dict] = []
        last: UpstreamError | None = None
        for dep in self._attempt_order(p, ctx):
            t = time.perf_counter()
            try:
                if dep.provider.name in ctx.options.simulate_outage:
                    raise SimulatedOutage(dep)
                data = await self.client.chat(dep, p.payload)
            except UpstreamError as e:
                ms = (time.perf_counter() - t) * 1000
                attempts.append(
                    {"deployment": dep.id, "ok": False, "ms": round(ms, 1), "status": e.status, "error": str(e)[:160]}
                )
                last = e
                if not e.retryable:
                    break  # the request itself is invalid: another model would reject it too
                if not isinstance(e, SimulatedOutage) and not e.model_fault:
                    await self.router.record(dep, False, ms, p.health.get(dep.id))
                continue
            ms = (time.perf_counter() - t) * 1000
            await self.router.record(dep, True, ms, p.health.get(dep.id))
            attempts.append({"deployment": dep.id, "ok": True, "ms": round(ms, 1), "status": 200})
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            raw = msg.get("content") or ""
            await self._store(p, raw, dep.id, vec)
            msg["content"] = restore(raw, p.redaction) if raw else msg.get("content")
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if isinstance(fn.get("arguments"), str):
                    fn["arguments"] = restore(fn["arguments"], p.redaction)
            usage = data.get("usage") or {}
            meta = self._meta(ctx, p, t0, dep.id, None, attempts)
            await self._record(
                self._event(
                    ctx,
                    p,
                    payload,
                    t0,
                    served_by=dep.id,
                    status=200,
                    upstream_ms=round(ms, 1),
                    cache=None,
                    similarity=None,
                    attempts=attempts,
                    tokens_in=int(usage.get("prompt_tokens") or 0),
                    tokens_out=int(usage.get("completion_tokens") or 0),
                )
            )
            data["id"] = ctx.request_id
            data["model"] = dep.id
            data["sluice"] = meta
            return data

        status = last.status if last and last.status in CLIENT_ERRORS else 503
        msg = str(last) if last else "no provider available"
        await self._record(
            self._event(
                ctx,
                p,
                payload,
                t0,
                served_by=None,
                status=status,
                upstream_ms=None,
                cache=None,
                similarity=None,
                attempts=attempts,
                error=msg[:300],
            )
        )
        raise GatewayError(
            status,
            f"all providers failed: {msg}" if status == 503 else msg,
            "upstream_unavailable" if status == 503 else "upstream_rejected",
        )

    # ------------------------------------------------------------------ streaming
    async def stream(self, payload: dict, ctx: Context) -> AsyncIterator[dict]:
        """Events: {"type": "step"|"delta"|"chunk"|"done"|"error", ...}."""
        t0 = time.perf_counter()
        try:
            p = await self.prepare(payload, ctx)
        except GatewayError as e:
            await self._record(
                self._event(
                    ctx,
                    None,
                    payload,
                    t0,
                    served_by=None,
                    status=e.status,
                    upstream_ms=None,
                    cache=None,
                    similarity=None,
                    stream=True,
                    error=str(e),
                )
            )
            yield {"type": "error", "status": e.status, "code": e.code, "message": str(e)}
            return
        for s in p.steps:
            yield {"type": "step", **s}
        seen = len(p.steps)
        try:
            hit, vec = await self._screen_and_lookup(p, ctx)
        except GatewayError as e:
            for s in p.steps[seen:]:
                yield {"type": "step", **s}
            await self._record(
                self._event(
                    ctx,
                    p,
                    payload,
                    t0,
                    served_by=None,
                    status=e.status,
                    upstream_ms=None,
                    cache=None,
                    similarity=None,
                    stream=True,
                    error=str(e),
                )
            )
            yield {"type": "error", "status": e.status, "code": e.code, "message": str(e)}
            return
        for s in p.steps[seen:]:
            yield {"type": "step", **s}
        if hit:
            content = restore(hit.content, p.redaction)
            for i in range(0, len(content), 24):
                yield {"type": "delta", "text": content[i : i + 24], "model": hit.source_model}
            meta = self._meta(ctx, p, t0, hit.source_model, hit.kind, [], round(hit.similarity, 4))
            await self._record(
                self._event(
                    ctx,
                    p,
                    payload,
                    t0,
                    served_by=None,
                    status=200,
                    upstream_ms=None,
                    cache=hit.kind,
                    similarity=hit.similarity,
                    stream=True,
                    tokens_out=_approx_tokens(content),
                )
            )
            yield {"type": "done", "meta": meta, "usage": None}
            return

        attempts: list[dict] = []
        last: UpstreamError | None = None
        for dep in self._attempt_order(p, ctx):
            t = time.perf_counter()
            restorer = StreamRestorer(p.redaction)
            parts: list[str] = []
            usage = None
            first_ms = None
            try:
                if dep.provider.name in ctx.options.simulate_outage:
                    raise SimulatedOutage(dep)
                async for chunk in self.client.stream(dep, p.payload):
                    if first_ms is None:
                        first_ms = (time.perf_counter() - t) * 1000
                        yield {"type": "step", "step": "upstream", "deployment": dep.id, "ttft_ms": round(first_ms, 1)}
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for ch in chunk.get("choices") or []:
                        delta = ch.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            parts.append(piece)
                            out = restorer.feed(piece)
                            if out:
                                yield {"type": "delta", "text": out, "model": dep.id}
                        if delta.get("tool_calls") or ch.get("finish_reason"):
                            yield {"type": "chunk", "chunk": chunk, "model": dep.id}
            except UpstreamError as e:
                ms = (time.perf_counter() - t) * 1000
                attempts.append(
                    {"deployment": dep.id, "ok": False, "ms": round(ms, 1), "status": e.status, "error": str(e)[:160]}
                )
                yield {
                    "type": "step",
                    "step": "attempt_failed",
                    "deployment": dep.id,
                    "status": e.status,
                    "ms": round(ms, 1),
                    "simulated": isinstance(e, SimulatedOutage),
                }
                last = e
                if not e.retryable:
                    break  # the request itself is invalid: another model would reject it too
                if not isinstance(e, SimulatedOutage) and not e.model_fault:
                    await self.router.record(dep, False, ms, p.health.get(dep.id))
                continue
            tail = restorer.flush()
            if tail:
                yield {"type": "delta", "text": tail, "model": dep.id}
            ms = (time.perf_counter() - t) * 1000
            await self.router.record(dep, True, first_ms or ms, p.health.get(dep.id))
            attempts.append(
                {
                    "deployment": dep.id,
                    "ok": True,
                    "ms": round(first_ms or ms, 1),
                    "status": 200,
                    "total_ms": round(ms, 1),
                }
            )
            full = "".join(parts)
            await self._store(p, full, dep.id, vec)
            meta = self._meta(ctx, p, t0, dep.id, None, attempts)
            u = usage or {}
            await self._record(
                self._event(
                    ctx,
                    p,
                    payload,
                    t0,
                    served_by=dep.id,
                    status=200,
                    upstream_ms=round(ms, 1),
                    cache=None,
                    similarity=None,
                    attempts=attempts,
                    stream=True,
                    tokens_in=int(u.get("prompt_tokens") or 0),
                    tokens_out=int(u.get("completion_tokens") or _approx_tokens(full)),
                )
            )
            yield {"type": "done", "meta": meta, "usage": usage}
            return

        status = last.status if last and last.status in CLIENT_ERRORS else 503
        msg = str(last) if last else "no provider available"
        await self._record(
            self._event(
                ctx,
                p,
                payload,
                t0,
                served_by=None,
                status=status,
                upstream_ms=None,
                cache=None,
                similarity=None,
                attempts=attempts,
                stream=True,
                error=msg[:300],
            )
        )
        yield {
            "type": "error",
            "status": status,
            "code": "upstream_unavailable",
            "message": f"all providers failed: {msg}",
        }


def _completion(rid: str, model: str, content: str, usage, meta: dict) -> dict:
    return {
        "id": rid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": usage
        or {"prompt_tokens": 0, "completion_tokens": _approx_tokens(content), "total_tokens": _approx_tokens(content)},
        "sluice": meta,
    }
