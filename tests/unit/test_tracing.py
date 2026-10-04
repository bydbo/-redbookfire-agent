"""调用追踪门面的单测（S4.2）：离线、不联网、不构造真客户端。

假客户端实现了 Langfuse SDK 用到的四个方法，并记录 span 嵌套层级；
这样"上报了什么字段"可以逐字断言，而不依赖云服务。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

import pytest

from xhs_agent.config import AppConfig, EnvView, load_config
from xhs_agent.core import tracing
from xhs_agent.core.tracing import (
    Generation,
    LangfuseTracer,
    NullTracer,
    TracingProvider,
    availability,
    capture_content,
    get_tracer,
    reset_tracer,
)

KEYS = {"LANGFUSE_PUBLIC_KEY": "pk-lf-test", "LANGFUSE_SECRET_KEY": "sk-lf-test",
        "LANGFUSE_HOST": "https://cloud.langfuse.com"}


def make_config(tmp_path, env: dict[str, str] | None = None) -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    cfg = load_config(str(path))
    cfg._env = EnvView({}, dict(env or {}))
    return cfg


class FakeObservation:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = dict(kwargs)


class FakeClient:
    """记录 span 的父子层级与收尾字段（模拟 Langfuse SDK 的四个方法）。"""

    def __init__(self) -> None:
        self.stack: list[FakeObservation] = []
        self.observations: list[dict[str, Any]] = []   # 含 parent 深度、入参
        self.generation_updates: list[dict[str, Any]] = []
        self.span_updates: list[dict[str, Any]] = []
        self.flushes = 0
        self.fail_updates = False

    @contextmanager
    def start_as_current_observation(self, **kwargs: Any):
        observation = FakeObservation(kwargs)
        self.observations.append({"parent": len(self.stack), **{k: v for k, v in kwargs.items()
                                                               if k != "metadata"},
                                  "metadata": dict(kwargs.get("metadata") or {})})
        self.stack.append(observation)
        try:
            yield observation
        finally:
            self.stack.pop()

    def update_current_generation(self, **kwargs: Any) -> None:
        if self.fail_updates:
            raise RuntimeError("假装上报失败")
        self.generation_updates.append(dict(kwargs))

    def update_current_span(self, **kwargs: Any) -> None:
        if self.fail_updates:
            raise RuntimeError("假装上报失败")
        self.span_updates.append(dict(kwargs))

    def flush(self) -> None:
        self.flushes += 1

    def get_current_trace_id(self) -> str:
        return "trace-0123456789abcdef"

    def get_trace_url(self, *, trace_id: str | None = None) -> str:
        return f"https://cloud.langfuse.com/trace/{trace_id}"


class FakeResult:
    """与 `tools.llm.LLMResult` 同形（TracingProvider 只鸭子类型依赖它）。"""

    def __init__(self, *, prompt_tokens: int = 100, completion_tokens: int = 50,
                 error: str = "", text: str = "正文") -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.error = error
        self.text = text
        self.attempts = 1
        self.model = "fake-1"


class FakeCall:
    task = "hotspot_clue"
    system = "系统提示"
    user = "用户输入"


class FakeInnerProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self, *, result: FakeResult | None = None,
                 error: Exception | None = None) -> None:
        self.result = result or FakeResult()
        self.error = error
        self.calls = 0

    async def complete(self, call: Any) -> FakeResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_tracer()
    yield
    reset_tracer()


class TestAvailability:
    def test_complete_keys_enable_tracing(self, tmp_path):
        assert availability(make_config(tmp_path, KEYS)) == (True, "")

    @pytest.mark.parametrize("missing", ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
                                         "LANGFUSE_HOST"])
    def test_missing_one_key_disables_with_reason(self, tmp_path, missing):
        env = {k: v for k, v in KEYS.items() if k != missing}
        enabled, reason = availability(make_config(tmp_path, env))
        assert enabled is False
        assert missing in reason and "config/.env" in reason

    @pytest.mark.parametrize(("value", "expected"), [
        ("", False), ("false", False), ("0", False), ("TRUE", True), ("yes", True),
        ("On", True), ("1", True),
    ])
    def test_capture_content_parsing(self, tmp_path, value, expected):
        env = dict(KEYS)
        if value:
            env["LANGFUSE_CAPTURE_CONTENT"] = value
        assert capture_content(make_config(tmp_path, env)) is expected


class TestNullTracerAndSingleton:
    def test_null_tracer_is_all_noops(self):
        tracer = NullTracer()
        assert tracer.enabled is False
        with (tracer.run(name="analyze", metadata={}),
              tracer.hotspot(index=1, metadata={}, content="热点"),
              tracer.generation(name="hotspot_clue", model="m", metadata={}) as fields):
            assert isinstance(fields, Generation)
        tracer.finish_run(status="succeeded", totals={})
        tracer.flush()

    def test_get_tracer_without_keys_warns_once(self, tmp_path, caplog):
        cfg = make_config(tmp_path)
        with caplog.at_level(logging.WARNING, logger="xhs_agent.tracing"):
            first = get_tracer(cfg)
            second = get_tracer(cfg)
        assert isinstance(first, NullTracer) and first is second
        warnings = [item for item in caplog.records if item.levelno >= logging.WARNING]
        assert len(warnings) == 1 and "LANGFUSE_PUBLIC_KEY" in warnings[0].getMessage()

    def test_warn_if_disabled_reuses_the_one_warning(self, tmp_path, caplog):
        cfg = make_config(tmp_path)
        with caplog.at_level(logging.WARNING, logger="xhs_agent.tracing"):
            assert tracing.warn_if_disabled(cfg) is False
            assert isinstance(get_tracer(cfg), NullTracer)
        assert len([item for item in caplog.records if item.levelno >= logging.WARNING]) == 1

    def test_get_tracer_builds_client_once(self, tmp_path, monkeypatch):
        cfg = make_config(tmp_path, KEYS)
        built: list[Any] = []
        monkeypatch.setattr(tracing, "build_client", lambda _cfg: built.append(FakeClient())
                              or built[-1])
        first = get_tracer(cfg)
        second = get_tracer(cfg)
        assert isinstance(first, LangfuseTracer) and first is second
        assert len(built) == 1

    def test_client_init_failure_falls_back_to_null(self, tmp_path, monkeypatch, caplog):
        cfg = make_config(tmp_path, KEYS)

        def boom(_cfg):
            raise RuntimeError("没网")

        monkeypatch.setattr(tracing, "build_client", boom)
        with caplog.at_level(logging.WARNING, logger="xhs_agent.tracing"):
            assert isinstance(get_tracer(cfg), NullTracer)
        assert any("初始化失败" in item.getMessage() for item in caplog.records)


class TestLangfuseTracerShape:
    def _tracer(self, tmp_path, *, capture: bool = False) -> tuple[LangfuseTracer, FakeClient]:
        env = dict(KEYS)
        if capture:
            env["LANGFUSE_CAPTURE_CONTENT"] = "true"
        client = FakeClient()
        return LangfuseTracer(make_config(tmp_path, env), client=client), client

    def test_run_and_hotspot_nesting_without_content(self, tmp_path):
        tracer, client = self._tracer(tmp_path)
        with (tracer.run(name="analyze", metadata={"run_id": "run-1", "hotspots": 1}),
              tracer.hotspot(index=1, metadata={"chars": 4}, content="热点原文")):
            pass

        first, second = client.observations
        assert first["name"] == "analyze" and first["as_type"] == "span"
        assert first["metadata"] == {"run_id": "run-1", "hotspots": 1}
        assert second["name"] == "hotspot-1" and second["parent"] == 1
        assert second["metadata"] == {"chars": 4}
        assert second["input"] is None          # 默认不上报正文（ADR 0005）

    def test_capture_content_sends_hotspot_text(self, tmp_path):
        tracer, client = self._tracer(tmp_path, capture=True)
        with (tracer.run(name="analyze", metadata={}),
              tracer.hotspot(index=2, metadata={}, content="热点原文")):
            pass
        assert client.observations[1]["input"] == "热点原文"

    def test_generation_reports_usage_cost_and_level(self, tmp_path):
        tracer, client = self._tracer(tmp_path)
        with (tracer.run(name="analyze", metadata={}),
              tracer.hotspot(index=1, metadata={}),
              tracer.generation(name="hotspot_clue", model="fake-1",
                                metadata={"provider": "fake"}) as fields):
            fields.usage_details = {"input": 100, "output": 50, "total": 150}
            fields.cost_details = {"input": 0.1, "output": 0.2, "total": 0.3}
            fields.metadata["attempts"] = 2
            fields.level = "ERROR"
            fields.status_message = "上游 503"

        generation = client.observations[2]
        assert generation["as_type"] == "generation" and generation["parent"] == 2
        assert generation["model"] == "fake-1"
        assert client.generation_updates[0]["usage_details"] == {"input": 100, "output": 50,
                                                                "total": 150}
        assert client.generation_updates[0]["cost_details"]["total"] == pytest.approx(0.3)
        assert client.generation_updates[0]["level"] == "ERROR"
        assert client.generation_updates[0]["status_message"] == "上游 503"
        assert "input" not in client.generation_updates[0]      # 默认不带正文

    def test_finish_run_writes_totals_to_trace(self, tmp_path):
        tracer, client = self._tracer(tmp_path)
        with tracer.run(name="analyze", metadata={}):
            tracer.finish_run(status="failed", totals={"llm_calls": 3, "prompt_tokens": 300,
                                                      "completion_tokens": 150,
                                                      "cost_cny": 0.0003, "latency_ms": 1200},
                              run_id="run-9", prompt_versions={"hotspot_clue": 1},
                              error="上游 503")
        payload = client.span_updates[0]
        assert payload["metadata"]["status"] == "failed"
        assert payload["metadata"]["llm_calls"] == 3
        assert payload["metadata"]["cost_cny"] == pytest.approx(0.0003)
        assert payload["metadata"]["run_id"] == "run-9"
        assert payload["metadata"]["prompt_versions"] == {"hotspot_clue": 1}
        assert payload["level"] == "ERROR" and payload["status_message"] == "上游 503"

    def test_finish_run_logs_trace_link(self, tmp_path, caplog):
        """运行结束时日志里带 trace 链接，便于按 run_id 回放（S4.2）。"""
        tracer, _client = self._tracer(tmp_path)
        with (caplog.at_level(logging.INFO, logger="xhs_agent.tracing"),
              tracer.run(name="analyze", metadata={})):
            tracer.finish_run(status="succeeded", totals={}, run_id="run-7")
        assert any("trace-0123456789abcdef" in item.getMessage()
                   and "run-7" in item.getMessage() for item in caplog.records)

    def test_update_failures_are_swallowed(self, tmp_path, caplog):
        tracer, client = self._tracer(tmp_path)
        client.fail_updates = True
        with caplog.at_level(logging.WARNING, logger="xhs_agent.tracing"):
            with (tracer.run(name="analyze", metadata={}),
                  tracer.generation(name="hotspot_clue", model="m", metadata={}) as fields):
                fields.usage_details = {"input": 1, "output": 1, "total": 2}
            tracer.finish_run(status="succeeded", totals={})
            tracer.flush()
        assert client.flushes == 1                      # flush 照样调用
        assert len(caplog.records) >= 2                 # 两处失败各留一条 warning


class TestTracingProvider:
    async def _call(self, tmp_path, *, capture: bool = False, result=None, error=None):
        env = dict(KEYS)
        if capture:
            env["LANGFUSE_CAPTURE_CONTENT"] = "true"
        tracer = LangfuseTracer(make_config(tmp_path, env), client=FakeClient())
        inner = FakeInnerProvider(result=result, error=error)
        provider = TracingProvider(inner, tracer, capture=capture,
                                   price_in_per_m=2.16, price_out_per_m=8.64)
        return provider, inner, tracer._client

    @pytest.mark.asyncio
    async def test_success_splits_cost_by_input_output(self, tmp_path):
        provider, inner, client = await self._call(tmp_path)
        result = await provider.complete(FakeCall())

        assert inner.calls == 1 and result.text == "正文"
        update = client.generation_updates[0]
        assert update["usage_details"] == {"input": 100, "output": 50, "total": 150}
        assert update["cost_details"]["input"] == pytest.approx(100 / 1e6 * 2.16)
        assert update["cost_details"]["output"] == pytest.approx(50 / 1e6 * 8.64)
        assert update["metadata"]["provider"] == "fake"
        assert "input" not in update and "output" not in update     # 默认不带正文
        assert provider.name == "fake" and provider.label == "fake:fake-1"

    @pytest.mark.asyncio
    async def test_error_result_marks_generation_error(self, tmp_path):
        provider, _inner, client = await self._call(tmp_path, result=FakeResult(error="上游 503"))
        await provider.complete(FakeCall())
        update = client.generation_updates[0]
        assert update["level"] == "ERROR" and update["status_message"] == "上游 503"

    @pytest.mark.asyncio
    async def test_exception_marks_generation_error_and_reraises(self, tmp_path):
        provider, _inner, client = await self._call(tmp_path, error=RuntimeError("炸了"))
        with pytest.raises(RuntimeError):
            await provider.complete(FakeCall())
        update = client.generation_updates[0]
        assert update["level"] == "ERROR" and "炸了" in update["status_message"]

    @pytest.mark.asyncio
    async def test_capture_content_includes_prompt_and_output(self, tmp_path):
        provider, _inner, client = await self._call(tmp_path, capture=True)
        await provider.complete(FakeCall())
        update = client.generation_updates[0]
        assert update["input"] == {"system": "系统提示", "user": "用户输入"}
        assert update["output"] == "正文"
