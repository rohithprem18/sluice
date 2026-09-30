"""Two-level response cache.

1. Exact: a hash of the full (redacted) request -> stored response, in Redis.
2. Semantic: the last user message is embedded and matched against earlier
   questions asked in the same *scope* (same model tier, same system prompt,
   same prior turns). A hit needs cosine similarity >= the threshold, so a
   paraphrase is served from cache while a different question is not.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass

import httpx

from sluice.embeddings import Embedder, EmbeddingUnavailable, cosine
from sluice.store import Store

NAMESPACE = "sluice-cache"

# Similarity scales differ between embedding models, so each has its own threshold,
# chosen on GLUE QQP for ~92-93% hit precision (bench/cache_eval.py, bench/results/).
CALIBRATED_THRESHOLDS = {
    "gemini-embedding-2": 0.97,
    "gemini-embedding-2-preview": 0.97,
    "gemini-embedding-001": 0.98,
}
DEFAULT_THRESHOLD = 0.98
MAX_CACHED_CHARS = 20_000


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def exact_key(model: str, payload: dict) -> str:
    keep = {k: payload.get(k) for k in ("messages", "temperature", "top_p", "max_tokens", "response_format", "stop")}
    return "sluice:cache:exact:" + hashlib.sha256(_canon({"model": model, **keep}).encode()).hexdigest()


def scope_of(model: str, messages: list[dict]) -> tuple[str, str]:
    """(scope id, question). Scope = model + everything before the last user turn."""
    last = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    question = _text(messages[last].get("content")) if last >= 0 else ""
    prefix = [{"role": m.get("role"), "content": _text(m.get("content"))} for m in messages[:last]]
    scope = hashlib.sha256(_canon({"model": model, "prefix": prefix}).encode()).hexdigest()[:24]
    return scope, question


@dataclass
class CacheHit:
    kind: str  # "exact" | "semantic"
    content: str
    similarity: float
    source_model: str
    matched_question: str = ""


class VectorIndex:
    async def query(self, vector: list[float], scope: str) -> tuple[float, dict] | None:
        raise NotImplementedError

    async def upsert(self, vector: list[float], meta: dict) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


class UpstashVectorIndex(VectorIndex):
    def __init__(self, url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self._url = url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(5.0), transport=transport
        )

    async def query(self, vector, scope):
        r = await self._client.post(
            f"{self._url}/query/{NAMESPACE}",
            json={"vector": vector, "topK": 1, "includeMetadata": True, "filter": f"scope = '{scope}'"},
        )
        r.raise_for_status()
        rows = r.json().get("result") or []
        return (float(rows[0]["score"]), rows[0].get("metadata") or {}) if rows else None

    async def upsert(self, vector, meta):
        r = await self._client.post(
            f"{self._url}/upsert/{NAMESPACE}", json={"id": uuid.uuid4().hex, "vector": vector, "metadata": meta}
        )
        r.raise_for_status()

    async def aclose(self):
        await self._client.aclose()


class MemoryVectorIndex(VectorIndex):
    def __init__(self):
        self.rows: list[tuple[list[float], dict]] = []

    async def query(self, vector, scope):
        best = None
        for vec, meta in self.rows:
            if meta.get("scope") != scope:
                continue
            s = cosine(vector, vec)
            # Upstash reports cosine as (1 + cos) / 2; mirror that so thresholds match
            s = (1 + s) / 2
            if best is None or s > best[0]:
                best = (s, meta)
        return best

    async def upsert(self, vector, meta):
        self.rows.append((vector, meta))


class ResponseCache:
    def __init__(
        self,
        store: Store,
        index: VectorIndex | None,
        embedder: Embedder | None,
        threshold: float | None = None,
        ttl_s: int = 7 * 24 * 3600,
    ):
        self.store, self.index, self.embedder = store, index, embedder
        self.threshold, self.ttl_s = threshold, ttl_s

    def threshold_for(self, embedding_model: str) -> float:
        """An explicit threshold overrides the calibrated per-model values."""
        if self.threshold is not None:
            return self.threshold
        return CALIBRATED_THRESHOLDS.get(embedding_model, DEFAULT_THRESHOLD)

    @property
    def semantic_enabled(self) -> bool:
        return self.index is not None and self.embedder is not None

    async def lookup(
        self, model: str, payload: dict, semantic: bool = True
    ) -> tuple[CacheHit | None, tuple[str, list[float]] | None]:
        """Returns (hit, embedding). The embedding (model tag, vector) is reused when storing."""
        raw = await self.store.cmd("GET", exact_key(model, payload))
        if raw:
            data = json.loads(raw)
            return CacheHit("exact", data["content"], 1.0, data.get("model", "")), None
        if not (semantic and self.semantic_enabled):
            return None, None
        scope, question = scope_of(model, payload["messages"])
        if not question.strip():
            return None, None
        try:
            tag, vec = await self.embedder.embed(question)
        except EmbeddingUnavailable:
            return None, None  # every embedding model is out of quota: exact matching only
        found = await self.index.query(vec, f"{scope}:{tag}")
        if found:
            score, meta = found
            fresh = time.time() - float(meta.get("created", 0)) < self.ttl_s
            if score >= self.threshold_for(tag) and fresh:
                hit = CacheHit(
                    "semantic", meta.get("content", ""), score, meta.get("model", ""), meta.get("question", "")
                )
                return hit, (tag, vec)
        return None, (tag, vec)

    async def store_response(
        self,
        model: str,
        payload: dict,
        content: str,
        served_by: str,
        embedding: tuple[str, list[float]] | None,
        semantic: bool = True,
    ) -> None:
        if not content or len(content) > MAX_CACHED_CHARS:
            return
        await self.store.cmd(
            "SET", exact_key(model, payload), json.dumps({"content": content, "model": served_by}), "EX", self.ttl_s
        )
        if not (semantic and self.semantic_enabled):
            return
        scope, question = scope_of(model, payload["messages"])
        if not question.strip():
            return
        if embedding is None:
            try:
                embedding = await self.embedder.embed(question)
            except EmbeddingUnavailable:
                return
        tag, vec = embedding
        meta = {
            "scope": f"{scope}:{tag}",
            "question": question[:500],
            "content": content,
            "model": served_by,
            "embedding_model": tag,
            "created": time.time(),
        }
        await self.index.upsert(vec, meta)
