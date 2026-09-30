import pytest

from sluice.router import classify_tier


def test_classify_tier():
    assert classify_tier({"messages": [{"role": "user", "content": "hi there"}]})[0] == "fast"
    assert classify_tier({"messages": [{"role": "user", "content": "Explain why B-trees suit disks"}]})[0] == "smart"
    assert classify_tier({"messages": [{"role": "user", "content": "x" * 2000}]})[0] == "smart"
    assert classify_tier({"messages": [{"role": "user", "content": "hi"}], "tools": [{}]})[0] == "smart"


@pytest.mark.asyncio
async def test_plan_orders_by_tier_and_pins(svc):
    plan = await svc.router.plan({"model": "sluice/fast", "messages": [{"role": "user", "content": "hi"}]})
    assert plan.tier == "fast" and plan.candidates[0].tier == "fast"
    plan = await svc.router.plan({"model": "gemini/gemini-3.8-flash", "messages": []})
    assert plan.candidates[0].id == "gemini/gemini-3.8-flash"


@pytest.mark.asyncio
async def test_unknown_model(svc):
    with pytest.raises(LookupError):
        await svc.router.plan({"model": "nope/model", "messages": []})


@pytest.mark.asyncio
async def test_breaker_opens_after_consecutive_failures(svc):
    dep = svc.deployments[0]
    for _ in range(svc.router.failures):
        await svc.router.record(dep, False, 100)
    health = await svc.router.health()
    assert health[dep.id].is_open
    plan = await svc.router.plan({"model": "sluice/smart", "messages": []})
    assert dep.id not in [d.id for d in plan.candidates] and dep.id in plan.skipped
    await svc.router.record(dep, True, 200)  # a success closes it again
    assert not (await svc.router.health())[dep.id].is_open


@pytest.mark.asyncio
async def test_latency_ewma_reorders(svc):
    fast = [d for d in svc.deployments if d.tier == "fast"]
    await svc.router.record(fast[0], True, 3000)
    await svc.router.record(fast[1], True, 200)
    plan = await svc.router.plan({"model": "sluice/fast", "messages": []})
    assert plan.candidates[0].id == fast[1].id


@pytest.mark.asyncio
async def test_tool_requests_skip_models_without_tool_calling(svc):
    plan = await svc.router.plan({"model": "sluice/smart", "messages": [], "tools": [{"type": "function"}]})
    assert plan.candidates and all(d.tools for d in plan.candidates)
    assert not any(d.provider.name == "openrouter" for d in plan.candidates)
