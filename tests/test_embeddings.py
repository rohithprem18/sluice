import httpx
import pytest

from sluice.embeddings import EmbeddingUnavailable, GeminiEmbedder


def transport(behaviour: dict[str, int]):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/")[-1].split(":")[0]
        calls.append(model)
        status = behaviour.get(model, 200)
        if status == 429:
            return httpx.Response(429, json={"error": {"message": "quota EmbedContentRequestsPerDayPerUser exceeded"}})
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"embedding": {"values": [1.0] + [0.0] * 767}})

    return httpx.MockTransport(handler), calls


@pytest.mark.asyncio
async def test_falls_back_to_next_model_when_quota_exhausted():
    t, calls = transport({"gemini-embedding-2": 429})
    e = GeminiEmbedder("k", transport=t)
    tag, vec = await e.embed("hello")
    assert tag == "gemini-embedding-001" and len(vec) == 768
    await e.embed("another")  # exhausted model is skipped without a call
    assert calls == ["gemini-embedding-2", "gemini-embedding-001", "gemini-embedding-001"]


@pytest.mark.asyncio
async def test_all_models_down_raises():
    t, _ = transport({m: 503 for m in ("gemini-embedding-2", "gemini-embedding-001", "gemini-embedding-2-preview")})
    with pytest.raises(EmbeddingUnavailable):
        await GeminiEmbedder("k", transport=t).embed("hello")
