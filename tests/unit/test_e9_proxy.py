"""E9 本地转发服务：预留、鉴权、usage 结算、缺 usage 与流式。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from xhs_agent.evaluation.budget import BudgetLedger, Price, PriceTable
from xhs_agent.evaluation.proxy import ProxyConfig, create_app

pytestmark = pytest.mark.asyncio


def _prices() -> PriceTable:
    return PriceTable(models={"m": Price(1.0, 2.0, 1000, 1000)}, source="test",
                      verified_at="2026-10-28")


def _config() -> ProxyConfig:
    return ProxyConfig(upstream_base_url="https://upstream.test/v1",
                       upstream_api_key="real-key", local_api_key="local-key")


async def _client(tmp_path: Path, handler, *, budget: float = 10.0):
    ledger = BudgetLedger(tmp_path / "ledger.json", budget_cny=budget, prices=_prices())
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(_config(), ledger, _prices(), client=upstream)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                               base_url="http://test",
                               headers={"Authorization": "Bearer local-key"})
    return client, ledger, upstream


async def test_chat_settles_and_replaces_auth(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"choices": [], "usage": {
            "prompt_tokens": 100, "completion_tokens": 50}})

    client, ledger, upstream = await _client(tmp_path, handler)
    try:
        response = await client.post("/v1/chat/completions",
                                     headers={"Authorization": "Bearer local-key"},
                                     json={"model": "m", "messages": []})
        assert response.status_code == 200
        assert seen == ["Bearer real-key"]
        summary = ledger.summary()
        assert summary["settled_cny"] == pytest.approx(0.0002)
        assert response.headers["x-e9-request-id"]
    finally:
        await client.aclose()
        await upstream.aclose()


async def test_unknown_model_is_rejected_without_upstream(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    client, _ledger, upstream = await _client(tmp_path, handler)
    try:
        response = await client.post("/v1/chat/completions",
                                     json={"model": "other", "messages": []})
        assert response.status_code == 403
        assert calls == 0
    finally:
        await client.aclose()
        await upstream.aclose()


async def test_budget_is_reserved_before_send(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1,
                                                   "completion_tokens": 1}})

    client, _ledger, upstream = await _client(tmp_path, handler, budget=0.001)
    try:
        response = await client.post("/v1/chat/completions",
                                     json={"model": "m", "messages": []})
        assert response.status_code == 402
        assert calls == 0
    finally:
        await client.aclose()
        await upstream.aclose()


async def test_missing_usage_becomes_unknown(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client, ledger, upstream = await _client(tmp_path, handler)
    try:
        response = await client.post("/v1/chat/completions",
                                     json={"model": "m", "messages": []})
        assert response.status_code == 200
        summary = ledger.summary()
        assert summary["unknown_cny"] == pytest.approx(0.003)
        assert summary["settled_cny"] == 0.0
    finally:
        await client.aclose()
        await upstream.aclose()


async def test_path_not_allowed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client, _ledger, upstream = await _client(tmp_path, handler)
    try:
        response = await client.post("/v1/images/generations", json={"model": "m"})
        assert response.status_code == 404
    finally:
        await client.aclose()
        await upstream.aclose()


async def test_stream_forwards_and_settles(tmp_path: Path) -> None:
    payload = ("data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}]}) + "\n\n"
               + "data: " + json.dumps({"usage": {"prompt_tokens": 10,
                                                  "completion_tokens": 5}}) + "\n\n"
               + "data: [DONE]\n\n")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload.encode("utf-8"),
                              headers={"content-type": "text/event-stream"})

    client, ledger, upstream = await _client(tmp_path, handler)
    try:
        response = await client.post("/v1/chat/completions",
                                     json={"model": "m", "messages": [], "stream": True})
        assert response.status_code == 200
        assert "[DONE]" in response.text
        summary = ledger.summary()
        assert summary["settled_cny"] == pytest.approx(0.00002)
    finally:
        await client.aclose()
        await upstream.aclose()
