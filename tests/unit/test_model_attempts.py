"""真实传输边界的 started / terminal、重试、修复和取消；仅 MockTransport。"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from xhs_agent.config import LLMConfig, VisionConfig
from xhs_agent.core import model_attempts
from xhs_agent.tools import llm, prompt
from xhs_agent.tools.vision import AsyncImageClient


def response(text: str = '{"ok": true}') -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                  "prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 40}})


@pytest.mark.asyncio
async def test_started_precedes_transport_and_retries_have_terminals(monkeypatch):
    events: list[tuple[str, dict[str, Any]]] = []

    async def observe(phase, facts):
        events.append((phase, facts))

    def transport(_request):
        assert events[-1][0] == "started"
        return httpx.Response(500, text="unavailable") if len(events) == 1 else response()

    monkeypatch.setattr(llm, "backoff_seconds", lambda _: 0)
    cfg = LLMConfig(base_url="https://text.test/v1", max_retries=1, prefix_usage_supported=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with model_attempts.observe(observe):
            result = await llm.OpenAICompatibleProvider(cfg, client=client).complete(
                llm.LLMCall(task="context_extract", system="s", user="u"))
    assert not result.error
    assert [phase for phase, _ in events] == ["started", "finished", "started", "finished"]
    assert len({facts["identity"] for _, facts in events}) == 1
    assert [facts["attempt"] for phase, facts in events if phase == "started"] == [1, 2]
    assert events[1][1]["status"] == "failed"
    assert events[-1][1]["cache_hit_tokens"] == 60
    assert events[0][1]["endpoint"] == "https://text.test/v1/chat/completions"
    assert all("messages" not in data and "user" not in data for _, data in events)


@pytest.mark.asyncio
async def test_structural_repairs_have_distinct_identities():
    events = []

    async def observe(phase, facts):
        events.append((phase, facts))

    count = 0

    def transport(_request):
        nonlocal count
        count += 1
        return response("not-json" if count == 1 else '{"ok": true}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = llm.OpenAICompatibleProvider(LLMConfig(base_url="https://text.test/v1"), client=client)
        with model_attempts.observe(observe):
            value, _ = await llm.StructuredCaller(provider).call("context_extract", "s", "u", parse=lambda x: x)
    assert value == {"ok": True}
    starts = [facts for phase, facts in events if phase == "started"]
    assert len(starts) == 2 and starts[0]["identity"] != starts[1]["identity"]


@pytest.mark.asyncio
async def test_cancelled_transport_is_unknown_terminal():
    events = []
    reached, gate = asyncio.Event(), asyncio.Event()

    async def observe(phase, facts):
        events.append((phase, facts))

    async def transport(_request):
        assert events[-1][0] == "started"
        reached.set()
        await gate.wait()
        return response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = llm.OpenAICompatibleProvider(LLMConfig(base_url="https://text.test/v1",
                                                        prefix_usage_supported=True), client=client)
        with model_attempts.observe(observe):
            task = asyncio.create_task(provider.complete(llm.LLMCall(task="context_extract", system="s", user="u")))
            await asyncio.wait_for(reached.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert [phase for phase, _ in events] == ["started", "finished"]
    assert events[-1][1]["status"] == "interrupted"
    assert events[-1][1]["usage_status"] == "unknown"
    assert events[-1][1]["cache_status"] == "incomplete"


@pytest.mark.asyncio
async def test_vision_uses_own_endpoint_capabilities_and_price():
    events = []

    async def observe(phase, facts):
        events.append((phase, facts))

    def transport(_request):
        assert events[-1][0] == "started"
        return response()

    cfg = VisionConfig(base_url="https://vision.test/v1", model="vision-local",
                       price_in_per_m=3.0, price_out_per_m=9.0, prefix_usage_supported=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with model_attempts.observe(observe):
            result = await AsyncImageClient(cfg, client=client).complete(
                prompt.render("image_hotspot_clue"), b"image", "image/png")
    assert result.cache_status == "complete"
    assert [phase for phase, _ in events] == ["started", "finished"]
    assert events[0][1]["model"] == "vision-local"
    assert events[0][1]["endpoint"] == "https://vision.test/v1/chat/completions"
    assert events[0][1]["price"]["input_per_m"] == 3.0
    assert events[-1][1]["usage_status"] == "actual"


@pytest.mark.asyncio
async def test_sync_vision_in_worker_thread_keeps_durable_event_order():
    events = []

    async def observe(phase, facts):
        events.append((phase, facts))

    def work():
        handle = model_attempts.begin_sync({"task": "material_tagging", "model": "fixed"})
        assert events[-1][0] == "started"
        handle.finish_sync({"status": "succeeded", "usage_status": "actual",
                            "prompt_tokens": 0, "completion_tokens": 0})
        assert events[-1][0] == "finished"

    with model_attempts.observe(observe):
        await asyncio.wait_for(asyncio.to_thread(work), 3)
    assert [phase for phase, _ in events] == ["started", "finished"]
    assert events[0][1]["identity"] == events[1][1]["identity"]
    assert events[1][1]["prompt_tokens"] == 0
