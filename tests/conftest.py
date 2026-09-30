"""Fakes: scripted upstream providers, a controllable embedder and a stub guard."""

from __future__ import annotations

import json
from collections import defaultdict

import httpx
import pytest

from sluice.app import Services, set_services
from sluice.cache import MemoryVectorIndex
from sluice.config import Settings
from sluice.embeddings import Embedder, normalize
from sluice.providers import OpenAIClient
from sluice.store import MemoryStore


class FakeUpstream:
    """httpx transport that plays scripted behaviour per model.

    script[model] is a list consumed per call; the last entry repeats. Entries:
    "ok", "empty", an int status code, or "timeout".
    """

    def __init__(self):
        self.script: dict[str, list] = defaultdict(lambda: ["ok"])
        self.calls: list[dict] = []

    def set(self, model: str, *behaviour):
        self.script[model] = list(behaviour)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append({"host": request.url.host, "body": body})
        seq = self.script[body["model"]]
        action = seq.pop(0) if len(seq) > 1 else seq[0]
        if action == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if isinstance(action, int):
            return httpx.Response(action, json={"error": {"message": f"scripted {action}"}})
        user = next((m["content"] for m in reversed(body["messages"]) if m["role"] == "user"), "")
        text = f"echo[{body['model']}]: {user}"
        if body.get("stream"):
            words = text.split(" ")
            lines = [
                json.dumps({"choices": [{"index": 0, "delta": {"content": w + (" " if i < len(words) - 1 else "")}}]})
                for i, w in enumerate(words)
            ]
            lines.append(json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
            lines.append(json.dumps({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": len(words)}}))
            sse = "".join(f"data: {x}\n\n" for x in lines) + "data: [DONE]\n\n"
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
            },
        )


class TableEmbedder(Embedder):
    """Maps known sentences to fixed directions; paraphrases share a direction."""

    def __init__(self, groups: list[list[str]]):
        self.vec: dict[str, list[float]] = {}
        for i, group in enumerate(groups):
            for text in group:
                v = [0.0] * 768
                v[i] = 1.0
                v[500] = 0.05  # tiny shared component so unrelated vectors are not exactly orthogonal
                self.vec[text] = normalize(v)

    async def embed(self, text: str) -> tuple[str, list[float]]:
        if text not in self.vec:
            v = [0.0] * 768
            v[700 - len(self.vec) % 100] = 1.0
            self.vec[text] = v
        return "table", self.vec[text]


class StubGuard:
    threshold = 0.9
    enabled = True

    async def score(self, text: str):
        return 0.99 if "ignore all previous instructions" in text.lower() else 0.01

    async def aclose(self):
        pass


PARAPHRASES = [
    ["What is the capital of Australia?", "Which city is Australia's capital?"],
    ["How do I reverse a list in Python?", "Python: how to reverse a list?"],
]


def make_settings(**over) -> Settings:
    base = dict(
        groq_api_key="g",
        gemini_api_key="m",
        openrouter_api_key="o",
        cerebras_api_key="",
        redis_url="",
        redis_token="",
        vector_url="",
        vector_token="",
        api_keys={},
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture
def upstream():
    return FakeUpstream()


@pytest.fixture
def svc(upstream):
    s = make_settings()
    client = OpenAIClient(transport=httpx.MockTransport(upstream.handler))
    services = Services(
        s,
        store=MemoryStore(),
        index=MemoryVectorIndex(),
        embedder=TableEmbedder(PARAPHRASES),
        client=client,
        guard=StubGuard(),
    )
    set_services(services)
    yield services
    set_services(None)
