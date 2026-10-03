"""Embedding 客户端单测：`httpx.MockTransport` 顶替真实端点，全离线、不联网、不读真实密钥。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import pytest_asyncio

from xhs_agent.config import EmbeddingConfig, EnvView, load_config
from xhs_agent.schemas import Element
from xhs_agent.tools import embedding

Handler = Callable[[dict[str, Any], int], httpx.Response]


def make_cfg(**overrides) -> EmbeddingConfig:
    base = {
        "base_url": "https://example.test/v1",
        "model": "text-embedding-v3",
        "dim": 1024,
        "batch_size": 10,
        "timeout_s": 5,
        "max_retries": 2,
    }
    base.update(overrides)
    cfg = EmbeddingConfig(**base)
    # 假密钥：httpx 会校验请求头，空 key 的 "Bearer " 在真实连接上会被拒（LocalProtocolError）
    cfg._env = EnvView({}, {cfg.api_key_env: "sk-test"})
    return cfg


def vector(value: float = 0.1, dim: int = 1024) -> list[float]:
    return [float(value)] * dim


def response_ok(n: int, value: float = 0.1) -> httpx.Response:
    return httpx.Response(200, json={
        "data": [{"index": i, "embedding": vector(value)} for i in range(n)],
        "usage": {"prompt_tokens": 12},
    })


class MockAPI:
    """用 `httpx.MockTransport` 顶替真实端点：记录每次请求，handler 决定返回或抛错。"""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        self.calls.append({
            "url": str(request.url),
            "payload": payload,
            "authorization": request.headers.get("authorization"),
            "timeout": request.extensions["timeout"]["read"],
        })
        return self.handler(payload, len(self.calls))

    async def aclose(self) -> None:
        await self.client.aclose()


@pytest_asyncio.fixture
async def make_api() -> AsyncIterator[Callable[[Handler], MockAPI]]:
    """按用例给的 handler 造 MockAPI，并在用例结束时统一关闭客户端。"""
    created: list[MockAPI] = []

    def _make(handler: Handler) -> MockAPI:
        api = MockAPI(handler)
        created.append(api)
        return api

    yield _make
    for api in created:
        await api.aclose()


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试退避在测试里变成立即返回，避免用例真的等 1.5s / 3s。"""
    monkeypatch.setattr(embedding, "backoff_seconds", lambda _attempt: 0.0)


def write_config(tmp_path, body: str) -> str:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


class TestMaterialEmbeddingText:
    def test_order_is_tags_title_description_element_values(self):
        text = embedding.material_embedding_text("标题", "描述", ["标签"], [{"value": "羽毛球"}])
        assert text == "标签 标题 描述 羽毛球"

    def test_dedupes_and_drops_blanks(self):
        text = embedding.material_embedding_text("标题", "", ["标题", " ", "标签"],
                                                 [{"value": "标签"}])
        assert text == "标题 标签"

    def test_accepts_element_objects(self):
        text = embedding.material_embedding_text("", "", None,
                                                 [Element(type="topic", value="球场")])
        assert text == "球场"

    def test_all_empty_returns_blank(self):
        assert embedding.material_embedding_text("", "", [], []) == ""
        assert embedding.material_embedding_text("", "", None, None) == ""

    def test_provider_batch_cap_is_pinned(self):
        # 实测 DashScope text-embedding-v3 兼容端点的硬上限；换供应商必须复核这里
        assert embedding.MAX_BATCH_PER_REQUEST == 10


class TestEmbed:
    @pytest.mark.asyncio
    async def test_request_shape_and_vector_order(self, make_api):
        def handler(payload, _n):
            count = len(payload["input"])
            return httpx.Response(200, json={
                "data": [{"index": i, "embedding": vector(float(i))}
                         for i in reversed(range(count))],
                "usage": {"prompt_tokens": 12}})

        api = make_api(handler)
        result = await embedding.EmbeddingClient(make_cfg(), client=api.client).embed(["a", "b"])
        call = api.calls[0]
        assert call["url"] == "https://example.test/v1/embeddings"
        assert call["payload"] == {"model": "text-embedding-v3", "input": ["a", "b"]}
        assert call["authorization"] == "Bearer sk-test"
        assert call["timeout"] == 5
        assert [item[0] for item in result.vectors] == [0.0, 1.0]  # index 乱序也要对回入参
        assert result.model == "text-embedding-v3"
        assert result.prompt_tokens == 12
        assert result.attempts == 1

    @pytest.mark.asyncio
    async def test_does_not_send_dimensions_parameter(self, make_api):
        api = make_api(lambda payload, _n: response_ok(len(payload["input"])))
        await embedding.EmbeddingClient(make_cfg(), client=api.client).embed(["a"])
        assert "dimensions" not in api.calls[0]["payload"]

    @pytest.mark.asyncio
    async def test_dim_mismatch_raises_with_contract_hint(self, make_api):
        api = make_api(lambda _payload, _n: httpx.Response(200, json={
            "data": [{"index": 0, "embedding": vector(dim=768)}]}))
        with pytest.raises(embedding.EmbeddingError, match="维度"):
            await embedding.EmbeddingClient(make_cfg(), client=api.client).embed(["a"])

    @pytest.mark.asyncio
    async def test_count_mismatch_raises(self, make_api):
        api = make_api(lambda _payload, _n: httpx.Response(200, json={
            "data": [{"index": 0, "embedding": vector()}]}))
        with pytest.raises(embedding.EmbeddingError, match="条数"):
            await embedding.EmbeddingClient(make_cfg(), client=api.client).embed(["a", "b"])

    @pytest.mark.asyncio
    async def test_malformed_body_raises(self, make_api):
        api = make_api(lambda _payload, _n: httpx.Response(200, json={"nope": 1}))
        with pytest.raises(embedding.EmbeddingError, match="返回结构异常"):
            await embedding.EmbeddingClient(make_cfg(), client=api.client).embed(["a"])

    @pytest.mark.asyncio
    async def test_empty_input_makes_no_request(self, make_api):
        api = make_api(lambda _payload, _n: httpx.Response(200, json={}))
        result = await embedding.EmbeddingClient(make_cfg(), client=api.client).embed([])
        assert api.calls == []
        assert result.vectors == []

    @pytest.mark.asyncio
    async def test_retry_then_success(self, make_api, no_backoff):
        def handler(payload, n):
            if n == 1:
                raise httpx.ConnectError("boom")
            return response_ok(len(payload["input"]))

        api = make_api(handler)
        result = await embedding.EmbeddingClient(make_cfg(), client=api.client).embed(["a"])
        assert len(api.calls) == 2
        assert result.attempts == 2

    @pytest.mark.asyncio
    async def test_all_attempts_fail_raises(self, make_api, no_backoff):
        def handler(_payload, _n):
            raise httpx.ConnectError("down")

        api = make_api(handler)
        with pytest.raises(embedding.EmbeddingError, match="已尝试 3 次"):
            await embedding.EmbeddingClient(make_cfg(), client=api.client).embed(["a"])
        assert len(api.calls) == 3  # 1 次 + max_retries=2

    @pytest.mark.asyncio
    async def test_http_400_is_not_retried(self, make_api, no_backoff):
        api = make_api(lambda _payload, _n: httpx.Response(
            400, json={"error": {"message": "bad input"}}))
        with pytest.raises(embedding.EmbeddingError, match="HTTP 400"):
            await embedding.EmbeddingClient(make_cfg(), client=api.client).embed(["a"])
        assert len(api.calls) == 1

    @pytest.mark.asyncio
    async def test_batching_splits_at_provider_cap(self, make_api):
        api = make_api(lambda payload, _n: response_ok(len(payload["input"])))
        client = embedding.EmbeddingClient(make_cfg(batch_size=16), client=api.client)
        result = await client.embed([f"t{i}" for i in range(16)])
        assert [len(call["payload"]["input"]) for call in api.calls] == [10, 6]
        assert len(result.vectors) == 16
        assert result.attempts == 2


class TestBuildEmbedder:
    @pytest.mark.asyncio
    async def test_disabled_returns_none(self, tmp_path, make_api):
        api = make_api(lambda _payload, _n: httpx.Response(200, json={}))
        cfg = load_config(write_config(tmp_path, "[embedding]\nenabled = false\n"))
        assert embedding.build_embedder(cfg, client=api.client) is None

    @pytest.mark.asyncio
    async def test_enabled_without_key_raises(self, tmp_path, make_api):
        api = make_api(lambda _payload, _n: httpx.Response(200, json={}))
        cfg = load_config(write_config(tmp_path, "[embedding]\nenabled = true\n"))
        with pytest.raises(embedding.EmbeddingError, match="DASHSCOPE_API_KEY"):
            embedding.build_embedder(cfg, client=api.client)

    @pytest.mark.asyncio
    async def test_enabled_with_key_builds_client(self, tmp_path, make_api):
        api = make_api(lambda _payload, _n: httpx.Response(200, json={}))
        path = write_config(tmp_path, "[embedding]\nenabled = true\n")
        (tmp_path / ".env").write_text("DASHSCOPE_API_KEY=sk-test\n", encoding="utf-8")
        client = embedding.build_embedder(load_config(path), client=api.client)
        assert isinstance(client, embedding.EmbeddingClient)
        assert client.batch_size == 10
        assert client.label == "embedding:text-embedding-v3"
