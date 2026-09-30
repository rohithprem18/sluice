"""Upstream providers. All of them speak the OpenAI chat-completions protocol."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from sluice.config import Settings


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    label: str

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class Deployment:
    """One concrete model on one provider: the unit Sluice routes to."""

    provider: Provider
    model: str
    tier: str  # "smart" | "fast"
    context: int = 32_000
    # request fields applied only when the caller did not set them, e.g. keeping
    # reasoning models from spending the whole max_tokens budget on hidden thinking
    defaults: tuple[tuple[str, object], ...] = ()
    tools: bool = True  # structured tool calling; requests with tools only go to deployments that support it

    @property
    def id(self) -> str:
        return f"{self.provider.name}/{self.model}"


class UpstreamError(Exception):
    def __init__(self, status: int, message: str, retryable: bool, model_fault: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        # the request was fine but this model could not serve it (e.g. a malformed tool call);
        # another model can, and the provider's health should not suffer for it
        self.model_fault = model_fault


def providers(s: Settings) -> dict[str, Provider]:
    return {
        "groq": Provider("groq", "https://api.groq.com/openai/v1", s.groq_api_key, "Groq"),
        "gemini": Provider(
            "gemini", "https://generativelanguage.googleapis.com/v1beta/openai", s.gemini_api_key, "Google Gemini"
        ),
        "openrouter": Provider("openrouter", "https://openrouter.ai/api/v1", s.openrouter_api_key, "OpenRouter"),
        "cerebras": Provider("cerebras", "https://api.cerebras.ai/v1", s.cerebras_api_key, "Cerebras"),
    }


def catalog(s: Settings) -> list[Deployment]:
    """Every free deployment Sluice knows, in preference order within a tier."""
    p = providers(s)
    low = (("reasoning_effort", "low"),)
    rows = [
        Deployment(p["groq"], "openai/gpt-oss-120b", "smart", 131_072, low),
        Deployment(p["gemini"], "gemini-3.8-flash", "smart", 1_000_000, low),
        Deployment(p["groq"], "qwen/qwen3.8-27b", "smart", 131_072, (("reasoning_effort", "none"),)),
        Deployment(p["openrouter"], "nvidia/nemotron-3-super-120b-a12b:free", "smart", 131_072, tools=False),
        Deployment(p["groq"], "openai/gpt-oss-20b", "fast", 131_072, low),
        Deployment(p["gemini"], "gemini-3.6-flash", "fast", 1_000_000, low),
        Deployment(p["gemini"], "gemini-3.5-flash-lite", "fast", 1_000_000, low),
        Deployment(p["openrouter"], "google/gemma-4-31b-it:free", "fast", 131_072, tools=False),
        Deployment(p["cerebras"], "gpt-oss-120b", "smart", 65_536, low),
    ]
    return [d for d in rows if d.provider.enabled]


# 400s that describe a model limitation rather than a bad request
MODEL_FAULTS = (
    "tool_use_failed",
    "thought_signature",
    "does not support tools",
    "tool calling is not supported",
    "function calling is not enabled",
    "failed_generation",
    "ending with a model turn",
)


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500 or status in (408, 409)


def _error(dep: Deployment, status: int, text: str) -> UpstreamError:
    model_fault = status in (400, 422) and any(m in text for m in MODEL_FAULTS)
    return UpstreamError(
        status, f"{dep.id} returned {status}: {text[:300]}", _retryable(status) or model_fault, model_fault
    )


# Gemini 3 requires a thought signature on every tool call in the conversation history.
# Calls made by another model (after a fallback) have none; Google documents this
# placeholder for tool calls Gemini did not generate.
GEMINI_PLACEHOLDER_SIGNATURE = "skip_thought_signature_validator"


def adapt_tool_history(provider: str, messages: list[dict]) -> list[dict]:
    """Make a multi-provider tool-calling history acceptable to `provider`."""
    if not any(m.get("tool_calls") for m in messages):
        return messages
    out = []
    for m in messages:
        calls = m.get("tool_calls")
        if not calls:
            out.append(m)
            continue
        fixed = []
        for tc in calls:
            tc = dict(tc)
            if provider == "gemini":
                google = dict((tc.get("extra_content") or {}).get("google") or {})
                google.setdefault("thought_signature", GEMINI_PLACEHOLDER_SIGNATURE)
                tc["extra_content"] = {**(tc.get("extra_content") or {}), "google": google}
            else:
                tc.pop("extra_content", None)  # Gemini-only field; strict providers reject it
            fixed.append(tc)
        out.append({**m, "tool_calls": fixed})
    return out


class OpenAIClient:
    """Minimal async client for any OpenAI-compatible endpoint."""

    def __init__(self, timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5.0),
            transport=transport,
            limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=120),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _headers(dep: Deployment) -> dict[str, str]:
        h = {"Authorization": f"Bearer {dep.provider.api_key}", "Content-Type": "application/json"}
        if dep.provider.name == "openrouter":
            h["HTTP-Referer"] = "https://github.com/rohithprem18/sluice"
            h["X-Title"] = "Sluice"
        return h

    @staticmethod
    def _body(dep: Deployment, payload: dict, stream: bool) -> dict:
        body = {k: v for k, v in payload.items() if k not in ("model", "stream", "stream_options")}
        for k, v in dep.defaults:
            body.setdefault(k, v)
        body["model"] = dep.model
        body["stream"] = stream
        if stream:
            body["stream_options"] = {"include_usage": True}
        if "messages" in body:
            body["messages"] = adapt_tool_history(dep.provider.name, body["messages"])
        return body

    async def chat(self, dep: Deployment, payload: dict) -> dict:
        try:
            r = await self._client.post(
                f"{dep.provider.base_url}/chat/completions",
                headers=self._headers(dep),
                json=self._body(dep, payload, False),
            )
        except httpx.TimeoutException as e:
            raise UpstreamError(504, f"{dep.id} timed out", True) from e
        except httpx.HTTPError as e:
            raise UpstreamError(502, f"{dep.id} unreachable: {e.__class__.__name__}", True) from e
        if r.status_code >= 400:
            raise _error(dep, r.status_code, r.text)
        data = r.json()
        if not data.get("choices"):
            raise UpstreamError(502, f"{dep.id} returned no choices", True)
        return data

    async def stream(self, dep: Deployment, payload: dict) -> AsyncIterator[dict]:
        """Yields parsed SSE chunks. Raises UpstreamError before the first chunk on failure."""
        req = self._client.build_request(
            "POST",
            f"{dep.provider.base_url}/chat/completions",
            headers=self._headers(dep),
            json=self._body(dep, payload, True),
        )
        try:
            r = await self._client.send(req, stream=True)
        except httpx.TimeoutException as e:
            raise UpstreamError(504, f"{dep.id} timed out", True) from e
        except httpx.HTTPError as e:
            raise UpstreamError(502, f"{dep.id} unreachable: {e.__class__.__name__}", True) from e
        try:
            if r.status_code >= 400:
                text = (await r.aread()).decode(errors="replace")
                raise _error(dep, r.status_code, text)
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue
        finally:
            await r.aclose()


def now_ms() -> float:
    return time.perf_counter() * 1000
