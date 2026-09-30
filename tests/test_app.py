import json

from fastapi.testclient import TestClient

from sluice.app import app

BODY = {
    "model": "sluice/smart",
    "messages": [{"role": "user", "content": "What is the capital of Australia?"}],
    "temperature": 0,
}


def test_models_lists_virtual_and_concrete(svc):
    ids = [m["id"] for m in TestClient(app).get("/v1/models").json()["data"]]
    assert "sluice/auto" in ids and "groq/openai/gpt-oss-120b" in ids


def test_chat_completion_json(svc):
    r = TestClient(app).post("/v1/chat/completions", json=BODY)
    assert r.status_code == 200
    assert r.headers["x-sluice-served-by"] == "groq/openai/gpt-oss-120b"
    assert r.json()["object"] == "chat.completion"


def test_api_keys_enforced(svc):
    svc.settings.api_keys["sk-test"] = "quarry"
    c = TestClient(app)
    assert c.post("/v1/chat/completions", json=BODY).status_code == 401
    ok = c.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer sk-test"})
    assert ok.status_code == 200
    svc.settings.api_keys.clear()


def test_sse_stream_format(svc):
    with TestClient(app).stream("POST", "/v1/chat/completions", json={**BODY, "stream": True}) as r:
        lines = [ln for ln in r.iter_lines() if ln.startswith("data:")]
    assert lines[-1] == "data: [DONE]"
    first = json.loads(lines[0][5:])
    assert first["object"] == "chat.completion.chunk" and first["choices"][0]["delta"]["role"] == "assistant"
    meta = json.loads(lines[-2][5:])
    assert meta["sluice"]["served_by"] == "groq/openai/gpt-oss-120b"


def test_stream_error_sets_status(svc):
    r = TestClient(app).post("/v1/chat/completions", json={**BODY, "model": "nope", "stream": True})
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"


def test_playground_ndjson_and_rate_limit(svc):
    svc.settings.__dict__["playground_per_hour"] = 2  # frozen dataclass: tests poke the instance
    c = TestClient(app)
    body = {"messages": [{"role": "user", "content": "hello"}]}
    r = c.post("/api/playground", json=body)
    events = [json.loads(x) for x in r.text.splitlines()]
    assert {e["type"] for e in events} >= {"step", "delta", "done"}
    assert c.post("/api/playground", json=body).status_code == 200
    assert c.post("/api/playground", json=body).status_code == 429


def test_redact_preview_and_stats_and_health(svc):
    c = TestClient(app)
    red = c.post("/api/redact", json={"text": "mail me at a@b.co"}).json()
    assert red["masked"] == "mail me at <EMAIL_1>" and red["found"] == {"EMAIL": 1}
    c.post("/v1/chat/completions", json=BODY)
    stats = c.get("/api/stats").json()
    assert stats["totals"]["requests"] == 1 and len(stats["series"]) == 60
    assert stats["deployments"][0]["state"] == "healthy"
    h = c.get("/api/health").json()
    assert h["backends"]["store"] == "memory" and h["deployments"] == len(svc.deployments)


def test_simulate_outage_header(svc):
    r = TestClient(app).post("/v1/chat/completions", json=BODY, headers={"x-sluice-simulate-outage": "groq, gemini"})
    assert r.status_code == 200 and r.headers["x-sluice-served-by"].startswith("openrouter/")
