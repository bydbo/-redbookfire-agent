"""Embedding 客户端单测：HTTP 传输注入假实现，全离线、不联网、不读真实密钥。"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from xhs_agent.config import EmbeddingConfig, load_config
from xhs_agent.schemas import Element
from xhs_agent.tools import embedding


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
    return EmbeddingConfig(**base)


def vector(value: float = 0.1, dim: int = 1024) -> list[float]:
    return [float(value)] * dim


def response_ok(n: int, value: float = 0.1) -> dict:
    return {"data": [{"index": i, "embedding": vector(value)} for i in range(n)],
            "usage": {"prompt_tokens": 12}}


class Recorder:
    """假传输：记录每次请求，交给 handler 决定返回或抛错。"""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict] = []

    def __call__(self, url, body, headers, timeout):
        payload = json.loads(body.decode("utf-8"))
        self.calls.append({"url": url, "payload": payload, "headers": headers,
                           "timeout": timeout})
        return self.handler(payload, len(self.calls))


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试退避在测试里变成立即返回，避免用例真的等 1.5s / 3s。"""
    monkeypatch.setattr(embedding.time, "sleep", lambda _seconds: None)


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
    def test_request_shape_and_vector_order(self):
        def handler(payload, _n):
            count = len(payload["input"])
            return {"data": [{"index": i, "embedding": vector(float(i))}
                             for i in reversed(range(count))],
                    "usage": {"prompt_tokens": 12}}

        recorder = Recorder(handler)
        result = embedding.EmbeddingClient(make_cfg(), transport=recorder).embed(["a", "b"])
        call = recorder.calls[0]
        assert call["url"] == "https://example.test/v1/embeddings"
        assert call["payload"] == {"model": "text-embedding-v3", "input": ["a", "b"]}
        assert call["headers"]["Authorization"] == "Bearer "
        assert call["timeout"] == 5
        assert [item[0] for item in result.vectors] == [0.0, 1.0]  # index 乱序也要对回入参
        assert result.model == "text-embedding-v3"
        assert result.prompt_tokens == 12
        assert result.attempts == 1

    def test_does_not_send_dimensions_parameter(self):
        recorder = Recorder(lambda payload, _n: response_ok(len(payload["input"])))
        embedding.EmbeddingClient(make_cfg(), transport=recorder).embed(["a"])
        assert "dimensions" not in recorder.calls[0]["payload"]

    def test_dim_mismatch_raises_with_contract_hint(self):
        recorder = Recorder(lambda payload, _n: {
            "data": [{"index": 0, "embedding": vector(dim=768)}]})
        with pytest.raises(embedding.EmbeddingError, match="维度"):
            embedding.EmbeddingClient(make_cfg(), transport=recorder).embed(["a"])

    def test_count_mismatch_raises(self):
        recorder = Recorder(lambda payload, _n: {"data": [{"index": 0, "embedding": vector()}]})
        with pytest.raises(embedding.EmbeddingError, match="条数"):
            embedding.EmbeddingClient(make_cfg(), transport=recorder).embed(["a", "b"])

    def test_malformed_body_raises(self):
        recorder = Recorder(lambda payload, _n: {"nope": 1})
        with pytest.raises(embedding.EmbeddingError, match="返回结构异常"):
            embedding.EmbeddingClient(make_cfg(), transport=recorder).embed(["a"])

    def test_empty_input_makes_no_request(self):
        recorder = Recorder(lambda payload, _n: {})
        result = embedding.EmbeddingClient(make_cfg(), transport=recorder).embed([])
        assert recorder.calls == []
        assert result.vectors == []

    def test_retry_then_success(self, no_sleep):
        def handler(payload, n):
            if n == 1:
                raise urllib.error.URLError("boom")
            return response_ok(len(payload["input"]))

        recorder = Recorder(handler)
        result = embedding.EmbeddingClient(make_cfg(), transport=recorder).embed(["a"])
        assert len(recorder.calls) == 2
        assert result.attempts == 2

    def test_all_attempts_fail_raises(self, no_sleep):
        def handler(_payload, _n):
            raise urllib.error.URLError("down")

        recorder = Recorder(handler)
        with pytest.raises(embedding.EmbeddingError, match="已尝试 3 次"):
            embedding.EmbeddingClient(make_cfg(), transport=recorder).embed(["a"])
        assert len(recorder.calls) == 3  # 1 次 + max_retries=2

    def test_http_400_is_not_retried(self, no_sleep):
        def handler(_payload, _n):
            raise urllib.error.HTTPError("https://example.test", 400, "Bad Request", {},
                                         io.BytesIO(b'{"error":{"message":"bad input"}}'))

        recorder = Recorder(handler)
        with pytest.raises(embedding.EmbeddingError, match="HTTP 400"):
            embedding.EmbeddingClient(make_cfg(), transport=recorder).embed(["a"])
        assert len(recorder.calls) == 1

    def test_batching_splits_at_provider_cap(self):
        def handler(payload, _n):
            return response_ok(len(payload["input"]))

        recorder = Recorder(handler)
        client = embedding.EmbeddingClient(make_cfg(batch_size=16), transport=recorder)
        result = client.embed([f"t{i}" for i in range(16)])
        assert [len(call["payload"]["input"]) for call in recorder.calls] == [10, 6]
        assert len(result.vectors) == 16
        assert result.attempts == 2


class TestBuildEmbedder:
    def test_disabled_returns_none(self, tmp_path):
        cfg = load_config(write_config(tmp_path, "[embedding]\nenabled = false\n"))
        assert embedding.build_embedder(cfg) is None

    def test_enabled_without_key_raises(self, tmp_path):
        cfg = load_config(write_config(tmp_path, "[embedding]\nenabled = true\n"))
        with pytest.raises(embedding.EmbeddingError, match="DASHSCOPE_API_KEY"):
            embedding.build_embedder(cfg)

    def test_enabled_with_key_builds_client(self, tmp_path):
        path = write_config(tmp_path, "[embedding]\nenabled = true\n")
        (tmp_path / ".env").write_text("DASHSCOPE_API_KEY=sk-test\n", encoding="utf-8")
        client = embedding.build_embedder(load_config(path))
        assert isinstance(client, embedding.EmbeddingClient)
        assert client.batch_size == 10
        assert client.label == "embedding:text-embedding-v3"
