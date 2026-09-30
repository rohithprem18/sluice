"""Text embeddings for the semantic cache (Gemini, 768 dimensions).

Free-tier embedding quotas are counted per model (1,000 requests a day each),
so the Gemini embedder walks a chain of models and moves on when one is
exhausted or rate limited. Vectors from different models are not comparable,
so every embedding is returned with the model that produced it and the cache
only matches entries from the same model.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import OrderedDict

import httpx

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MODELS = ("gemini-embedding-2", "gemini-embedding-001", "gemini-embedding-2-preview")
DIM = 768
EXHAUSTED_COOLDOWN_S = 3600


def normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False)) / (
        (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))) or 1.0
    )


class EmbeddingUnavailable(Exception):
    pass


class Embedder:
    dim = DIM
    name = "embedder"

    async def embed(self, text: str) -> tuple[str, list[float]]:
        """Returns (model tag, unit vector)."""
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


class GeminiEmbedder(Embedder):
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        models: tuple[str, ...] = GEMINI_MODELS,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_size: int = 512,
    ):
        self._key = api_key
        self.models = models
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0), transport=transport)
        self._lru: OrderedDict[str, tuple[str, list[float]]] = OrderedDict()
        self._size = cache_size
        self._resting: dict[str, float] = {}  # model -> retry-after timestamp

    async def embed(self, text: str) -> tuple[str, list[float]]:
        k = hashlib.sha256(text.encode()).hexdigest()
        if k in self._lru:
            self._lru.move_to_end(k)
            return self._lru[k]
        now = time.time()
        last_error = "no embedding model available"
        for model in self.models:
            if self._resting.get(model, 0) > now:
                continue
            try:
                r = await self._client.post(
                    f"{GEMINI_BASE}/{model}:embedContent",
                    params={"key": self._key},
                    json={
                        "content": {"parts": [{"text": text[:8000]}]},
                        "taskType": "SEMANTIC_SIMILARITY",
                        "outputDimensionality": DIM,
                    },
                )
            except httpx.HTTPError as e:
                last_error = f"{model}: {e.__class__.__name__}"
                continue
            if r.status_code == 429:
                daily = "PerDay" in r.text
                self._resting[model] = now + (EXHAUSTED_COOLDOWN_S if daily else 60)
                last_error = f"{model}: quota exhausted" if daily else f"{model}: rate limited"
                continue
            if r.status_code != 200:
                last_error = f"{model}: HTTP {r.status_code}"
                continue
            out = (model, normalize(r.json()["embedding"]["values"]))
            self._lru[k] = out
            if len(self._lru) > self._size:
                self._lru.popitem(last=False)
            return out
        raise EmbeddingUnavailable(last_error)

    async def aclose(self) -> None:
        await self._client.aclose()


class HashEmbedder(Embedder):
    """Deterministic bag-of-words embedding for tests and keyless local runs."""

    name = "hash"

    async def embed(self, text: str) -> tuple[str, list[float]]:
        v = [0.0] * DIM
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % DIM] += 1.0 if (h >> 12) & 1 else -1.0
        return "hash", normalize(v)
