"""调用追踪门面的单测（S4.2）：离线、不联网、不构造真客户端。

假客户端实现了 Langfuse SDK 用到的四个方法，并记录 span 嵌套层级；
这样"上报了什么字段"可以逐字断言，而不依赖云服务。
"""

from __future__ import annotations

import asyncio
import json
import logging
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


class FakeObservationContext:
    """假观测上下文（R1）：支持手动 enter / exit，并记录退出时收到的异常参数。

    `exit_args` 是 OBS-01/02 的关键证据：业务异常**绝不能**交给 SDK 的 `__exit__`
    （否则 SDK 底层的 OTel span 会自动 `record_exception` 把正文写进云端载荷）。
    """

    def __init__(self, client: FakeClient, kwargs: dict[str, Any]) -> None:
        self.client = client
        self.kwargs = dict(kwargs)

    def __enter__(self) -> FakeObservation:
        if self.client.fail_enter:
            raise RuntimeError("假装进入失败")
        observation = FakeObservation(self.kwargs)
        self.client.observations.append(
            {"parent": len(self.client.stack),
             **{k: v for k, v in self.kwargs.items() if k != "metadata"},
             "metadata": dict(self.kwargs.get("metadata") or {})})
        self.client.stack.append(observation)
        return observation

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.client.exit_args.append((exc_type, exc))
        self.client.stack.pop()
        if self.client.fail_exit:
            raise RuntimeError("假装退出失败")
        return False


class FakeClient:
    """记录 span 的父子层级与收尾字段（模拟 Langfuse SDK 的四个方法）。

    故障注入（R1 验收用）：`fail_create` 建观测抛错、`fail_enter` 进入抛错、
    `fail_exit` 退出抛错、`fail_updates` 上报抛错。
    """

    def __init__(self) -> None:
        self.stack: list[FakeObservation] = []
        self.observations: list[dict[str, Any]] = []   # 含 parent 深度、入参
        self.generation_updates: list[dict[str, Any]] = []
        self.span_updates: list[dict[str, Any]] = []
        self.exit_args: list[tuple[Any, Any]] = []     # 每次观测退出时收到的异常参数
        self.flushes = 0
        self.fail_create = False
        self.fail_enter = False
        self.fail_exit = False
        self.fail_updates = False

    def start_as_current_observation(self, **kwargs: Any) -> FakeObservationContext:
        if self.fail_create:
            raise RuntimeError("假装创建失败")
        return FakeObservationContext(self, kwargs)

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
                 error: str = "", text: str = "正文",
                 tool_calls: list[dict[str, Any]] | None = None,
                 usage_status: str = "") -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.error = error
        self.text = text
        self.attempts = 1
        self.model = "fake-1"
        self.tool_calls = list(tool_calls or [])
        self.usage_status = usage_status


class FakeCall:
    task = "hotspot_clue"
    system = "系统提示"
    user = "用户输入"
    stream = False


class StreamCall(FakeCall):
    """流式调用（S7.1 的 `stream=True` 路径）。"""

    stream = True


class FakeInnerProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self, *, result: FakeResult | None = None,
                 error: Exception | None = None) -> None:
        self.result = result or FakeResult()
        self.error = error
        self.calls = 0
        self.delta_calls = 0

    async def complete(self, call: Any) -> FakeResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result

    async def complete_with_tools(self, call: Any, on_delta: Any = None) -> FakeResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        for piece in ("增量", "文本"):
            if on_delta is not None:
                self.delta_calls += 1
                await on_delta(piece)
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

    def test_base_url_is_an_accepted_alias_for_host(self, tmp_path):
        """Langfuse 官方 SDK 同时认 LANGFUSE_HOST 与 LANGFUSE_BASE_URL，两个名字都给通。"""
        env = {k: v for k, v in KEYS.items() if k != "LANGFUSE_HOST"}
        env["LANGFUSE_BASE_URL"] = "https://cloud.langfuse.com"
        cfg = make_config(tmp_path, env)
        assert availability(cfg) == (True, "")
        assert cfg.langfuse_keys[2] == "https://cloud.langfuse.com"

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
              tracer.generation(name="hotspot_clue", model="m", metadata={}) as fields,
              tracer.turn(name="chat.turn", metadata={}, turn_id="turn-1"),
              tracer.tool(name="tool.x", metadata={}),
              tracer.node(name="node.clue", metadata={})):
            assert isinstance(fields, Generation)
        tracer.finish_run(status="succeeded", totals={})
        tracer.finish_turn(status="succeeded", totals={}, turn_id="turn-1")
        assert tracer.current_trace_id() == ""
        assert tracer.trace_url("trace-1") is None
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
            fields.status_message = "llm_error"   # 白名单内的安全类别

        generation = client.observations[2]
        assert generation["as_type"] == "generation" and generation["parent"] == 2
        assert generation["model"] == "fake-1"
        assert client.generation_updates[0]["usage_details"] == {"input": 100, "output": 50,
                                                                "total": 150}
        assert client.generation_updates[0]["cost_details"]["total"] == pytest.approx(0.3)
        assert client.generation_updates[0]["level"] == "ERROR"
        assert client.generation_updates[0]["status_message"] == "llm_error"
        assert "input" not in client.generation_updates[0]      # 默认不带正文

    def test_finish_run_writes_totals_to_trace(self, tmp_path):
        tracer, client = self._tracer(tmp_path)
        with tracer.run(name="analyze", metadata={}):
            tracer.finish_run(status="failed", totals={"llm_calls": 3, "prompt_tokens": 300,
                                                      "completion_tokens": 150,
                                                      "cost_cny": 0.0003, "latency_ms": 1200},
                              run_id="run-9", prompt_versions={"hotspot_clue": 1},
                              error="llm_error")
        payload = client.span_updates[0]
        assert payload["metadata"]["status"] == "failed"
        assert payload["metadata"]["llm_calls"] == 3
        assert payload["metadata"]["cost_cny"] == pytest.approx(0.0003)
        assert payload["metadata"]["run_id"] == "run-9"
        assert payload["metadata"]["prompt_versions"] == {"hotspot_clue": 1}
        assert payload["level"] == "ERROR" and payload["status_message"] == "llm_error"

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


class TestTurnSpans:
    """S7.19：对话轮次 / 工具 / 节点三层 span 与轮次收尾。"""

    def _tracer(self, tmp_path) -> tuple[LangfuseTracer, FakeClient]:
        client = FakeClient()
        return LangfuseTracer(make_config(tmp_path, KEYS), client=client), client

    def test_turn_tool_node_nesting_and_turn_id_metadata(self, tmp_path):
        tracer, client = self._tracer(tmp_path)
        with (tracer.turn(name="chat.turn", metadata={"session_id": "s1"}, turn_id="turn-9"),
              tracer.tool(name="tool.run_hotspot_analysis",
                          metadata={"tool": "run_hotspot_analysis"}),
              tracer.node(name="node.clue", metadata={"attempt": 1})):
            pass

        turn, tool, node = client.observations
        assert turn["name"] == "chat.turn" and turn["as_type"] == "span"
        assert turn["metadata"]["turn_id"] == "turn-9"
        assert tool["parent"] == 1 and tool["as_type"] == "span"
        assert node["parent"] == 2 and node["as_type"] == "span"

    def test_finish_turn_writes_totals_and_logs_link(self, tmp_path, caplog):
        tracer, client = self._tracer(tmp_path)
        with (caplog.at_level(logging.INFO, logger="xhs_agent.tracing"),
              tracer.turn(name="chat.turn", metadata={}, turn_id="turn-3")):
            tracer.finish_turn(status="succeeded",
                               totals={"tool_calls": 1, "llm_calls": 2, "prompt_tokens": 300,
                                       "completion_tokens": 120, "cost_cny": 0.0456,
                                       "latency_ms": 900, "usage_complete": False},
                               turn_id="turn-3")

        payload = client.span_updates[0]
        assert payload["metadata"]["status"] == "succeeded"
        assert payload["metadata"]["turn_id"] == "turn-3"
        assert payload["metadata"]["tool_calls"] == 1
        assert payload["metadata"]["cost_cny"] == pytest.approx(0.0456)
        assert payload["metadata"]["usage_complete"] is False
        assert any("turn-3" in item.getMessage()
                   and "trace-0123456789abcdef" in item.getMessage()
                   for item in caplog.records)

    def test_finish_turn_failure_is_swallowed(self, tmp_path, caplog):
        tracer, client = self._tracer(tmp_path)
        client.fail_updates = True
        with (caplog.at_level(logging.WARNING, logger="xhs_agent.tracing"),
              tracer.turn(name="chat.turn", metadata={})):
            tracer.finish_turn(status="failed", totals={}, error="坏了")
        assert any("turn 收尾失败" in item.getMessage() for item in caplog.records)

    def test_tool_and_node_update_failures_are_swallowed(self, tmp_path, caplog):
        """S7.22：建 span / 结束 span 失败只留 warning，业务照常走完（旁路隔离）。"""
        tracer, client = self._tracer(tmp_path)
        client.fail_updates = True
        with caplog.at_level(logging.WARNING, logger="xhs_agent.tracing"):
            with tracer.tool(name="tool.run_hotspot_analysis",
                             metadata={"tool": "run_hotspot_analysis"}) as fields:
                fields.level = "ERROR"
                fields.status_message = "tool_failed"
            with tracer.node(name="node.clue", metadata={"attempt": 1}) as fields:
                fields.status_message = "internal"
        assert sum("span 上报失败" in item.getMessage() for item in caplog.records) == 2

    def test_trace_id_and_url(self, tmp_path):
        tracer, _client = self._tracer(tmp_path)
        assert tracer.current_trace_id() == "trace-0123456789abcdef"
        assert tracer.trace_url("t-1") == "https://cloud.langfuse.com/trace/t-1"
        assert tracer.trace_url("") is None


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
        # OBS-02：返回错误正文不进载荷，只发固定安全类别
        assert update["level"] == "ERROR" and update["status_message"] == "llm_error"

    @pytest.mark.asyncio
    async def test_exception_marks_generation_error_and_reraises(self, tmp_path):
        provider, _inner, client = await self._call(tmp_path, error=RuntimeError("炸了"))
        with pytest.raises(RuntimeError):
            await provider.complete(FakeCall())
        update = client.generation_updates[0]
        # OBS-02：异常正文不进载荷，投影成安全类别（RuntimeError → internal）
        assert update["level"] == "ERROR" and update["status_message"] == "internal"

    @pytest.mark.asyncio
    async def test_capture_content_includes_prompt_and_output(self, tmp_path):
        provider, _inner, client = await self._call(tmp_path, capture=True)
        await provider.complete(FakeCall())
        update = client.generation_updates[0]
        assert update["input"] == {"system": "系统提示", "user": "用户输入"}
        assert update["output"] == "正文"

    @pytest.mark.asyncio
    async def test_complete_with_tools_passes_deltas_and_counts_tools(self, tmp_path):
        provider, inner, client = await self._call(
            tmp_path,
            result=FakeResult(tool_calls=[{"id": "1", "name": "x", "arguments": "{}"}]))
        deltas: list[str] = []

        async def on_delta(piece: str) -> None:
            deltas.append(piece)

        result = await provider.complete_with_tools(StreamCall(), on_delta=on_delta)
        assert result.text == "正文" and deltas == ["增量", "文本"]
        assert inner.calls == 1 and inner.delta_calls == 2
        update = client.generation_updates[0]
        assert update["metadata"]["tool_calls"] == 1
        assert update["metadata"]["stream"] is True
        assert update["usage_details"]["total"] == 150

    @pytest.mark.asyncio
    async def test_complete_with_tools_marks_exception_and_reraises(self, tmp_path):
        provider, _inner, client = await self._call(tmp_path, error=RuntimeError("流断了"))
        with pytest.raises(RuntimeError):
            await provider.complete_with_tools(FakeCall())
        update = client.generation_updates[0]
        assert update["level"] == "ERROR" and update["status_message"] == "internal"

    @pytest.mark.asyncio
    async def test_cancel_marks_generation_error_and_reraises(self, tmp_path):
        """取消（`CancelledError` 是 `BaseException`）也要收尾 generation，且原样抛出。"""
        provider, _inner, client = await self._call(tmp_path,
                                                    error=asyncio.CancelledError("取消"))
        with pytest.raises(asyncio.CancelledError):
            await provider.complete_with_tools(FakeCall())
        update = client.generation_updates[0]
        assert update["level"] == "ERROR"
        assert update["status_message"] == "cancelled"

    @pytest.mark.asyncio
    async def test_unknown_usage_is_marked_not_zeroed(self, tmp_path):
        provider, _inner, client = await self._call(
            tmp_path, result=FakeResult(prompt_tokens=0, completion_tokens=0, error="上游 503"))
        await provider.complete(FakeCall())
        assert client.generation_updates[0]["metadata"]["usage_status"] == "unknown"

    @pytest.mark.asyncio
    async def test_actual_usage_status_is_kept(self, tmp_path):
        provider, _inner, client = await self._call(
            tmp_path, result=FakeResult(usage_status="actual"))
        await provider.complete(FakeCall())
        assert client.generation_updates[0]["metadata"]["usage_status"] == "actual"


# ---------- R1（OBS-01）：安全观测上下文 ----------


class TestSafeObservationIsolation:
    """SDK 创建 / 进入 / 退出失败都不影响业务：业务块**只执行一次**、结果不变。"""

    SENTINEL = "SYNTHETIC_PRIVATE_TEXT /private/material.png sk-live-fake-credential"

    def _tracer(self, tmp_path, *, capture: bool = False):
        env = dict(KEYS)
        if capture:
            env["LANGFUSE_CAPTURE_CONTENT"] = "true"
        client = FakeClient()
        return LangfuseTracer(make_config(tmp_path, env), client=client), client

    def test_creation_failure_runs_business_once_for_all_six_observations(self, tmp_path,
                                                                         caplog):
        tracer, client = self._tracer(tmp_path)
        calls: list[str] = []
        with caplog.at_level(logging.WARNING, logger="xhs_agent.tracing"):
            with tracer.run(name="analyze", metadata={}):
                calls.append("run")
            with tracer.hotspot(index=1, metadata={}):
                calls.append("hotspot")
            with tracer.turn(name="chat.turn", metadata={}, turn_id="t-1"):
                calls.append("turn")
            with tracer.tool(name="tool.x", metadata={}) as fields:
                fields.level = "ERROR"
                calls.append("tool")
            with tracer.node(name="node.x", metadata={}) as fields:
                fields.status_message = "internal"
                calls.append("node")
            with tracer.generation(name="g", model="m", metadata={}) as fields:
                fields.usage_details = {"total": 1}
                calls.append("generation")
        before = (len(client.generation_updates), len(client.span_updates))
        client.fail_create = True
        with caplog.at_level(logging.WARNING, logger="xhs_agent.tracing"):
            with tracer.run(name="analyze", metadata={}):
                calls.append("run")
            with tracer.hotspot(index=1, metadata={}):
                calls.append("hotspot")
            with tracer.turn(name="chat.turn", metadata={}, turn_id="t-1"):
                calls.append("turn")
            with tracer.tool(name="tool.x", metadata={}) as fields:
                fields.status_message = "internal"
                calls.append("tool")
            with tracer.node(name="node.x", metadata={}) as fields:
                fields.status_message = "internal"
                calls.append("node")
            with tracer.generation(name="g", model="m", metadata={}) as fields:
                fields.usage_details = {"total": 1}
                fields.status_message = "llm_error"
                calls.append("generation")

        assert calls == ["run", "hotspot", "turn", "tool", "node", "generation"] * 2
        # 建观测失败：第二次没有多出任何字段写回（免得把更新误挂到外层 span）
        assert (len(client.generation_updates), len(client.span_updates)) == before
        assert sum("观测创建失败" in item.getMessage() for item in caplog.records) >= 6

    def test_enter_failure_is_isolated_like_creation_failure(self, tmp_path, caplog):
        tracer, client = self._tracer(tmp_path)
        client.fail_enter = True
        with (caplog.at_level(logging.WARNING, logger="xhs_agent.tracing"),
              tracer.tool(name="tool.x", metadata={}) as fields):
            fields.status_message = "internal"
            business = "done"
        assert business == "done"
        assert client.span_updates == [] and client.observations == []
        assert any("观测创建失败" in item.getMessage() for item in caplog.records)

    def test_exit_failure_keeps_business_result_and_updates(self, tmp_path, caplog):
        tracer, client = self._tracer(tmp_path)
        client.fail_exit = True
        with (caplog.at_level(logging.WARNING, logger="xhs_agent.tracing"),
              tracer.generation(name="g", model="m", metadata={}) as fields):
            fields.usage_details = {"total": 1}
            value = "业务结果"
        assert value == "业务结果"
        assert client.generation_updates[0]["usage_details"] == {"total": 1}
        assert any("观测收尾失败" in item.getMessage() for item in caplog.records)
        assert client.stack == []                        # 上下文已恢复

    def test_exit_failure_does_not_override_business_exception(self, tmp_path):
        tracer, client = self._tracer(tmp_path)
        client.fail_exit = True
        with (pytest.raises(ValueError, match="业务炸了"),
              tracer.run(name="analyze", metadata={})):
            raise ValueError("业务炸了")

    def test_cancel_propagates_and_observation_still_closes(self, tmp_path):
        tracer, client = self._tracer(tmp_path)
        with (pytest.raises(asyncio.CancelledError),
              tracer.turn(name="chat.turn", metadata={}, turn_id="t-2")):
            raise asyncio.CancelledError()
        assert client.stack == []
        assert client.exit_args == [(None, None)]        # 取消**不**交给 SDK

    def test_sdk_never_receives_business_exception(self, tmp_path):
        """OBS-02 的关键机制：业务异常不进 SDK 的 `__exit__`，底层 OTel 无从自动记录。"""
        tracer, client = self._tracer(tmp_path)
        with (pytest.raises(RuntimeError),
              tracer.generation(name="g", model="m", metadata={})):
            raise RuntimeError(self.SENTINEL)
        assert client.exit_args == [(None, None)]

    def test_fallback_turn_creation_failure_does_not_crash(self, tmp_path):
        """失败兜底再次建 turn（同 tracer）也不崩：六种观测一起故障注入跑两遍。"""
        tracer, client = self._tracer(tmp_path)
        client.fail_create = True
        closed = []
        with (tracer.turn(name="chat.turn", metadata={}, turn_id="t-3"),
              tracer.turn(name="chat.turn", metadata={"failed": True}, turn_id="t-3")):
            closed.append("fallback-ok")
        assert closed == ["fallback-ok"]


# ---------- R1（OBS-02）：安全错误投影（哨兵断言） ----------


class TestSafeErrorProjection:
    SENTINEL = "SYNTHETIC_PRIVATE_TEXT /private/material.png sk-live-fake-credential"

    def _tracer(self, tmp_path, *, capture: bool = False):
        env = dict(KEYS)
        if capture:
            env["LANGFUSE_CAPTURE_CONTENT"] = "true"
        client = FakeClient()
        return LangfuseTracer(make_config(tmp_path, env), client=client), client

    def _dump(self, client: FakeClient) -> str:
        return json.dumps({"spans": client.span_updates,
                           "generations": client.generation_updates}, ensure_ascii=False)

    def test_sentinel_never_reaches_handwritten_cloud_fields(self, tmp_path):
        """错误正文 / 路径 / 模拟凭据不落云端字段：run / turn / tool / node / generation 全覆盖。"""
        tracer, client = self._tracer(tmp_path)
        tracer.finish_run(status="failed", totals={}, run_id="r-1", error=self.SENTINEL)
        tracer.finish_turn(status="failed", totals={}, turn_id="t-1", error=self.SENTINEL)
        with tracer.tool(name="tool.x", metadata={}) as fields:
            fields.status_message = self.SENTINEL
        with tracer.node(name="node.x", metadata={}) as fields:
            fields.status_message = self.SENTINEL
        with tracer.generation(name="g", model="m", metadata={}) as fields:
            fields.status_message = self.SENTINEL

        dump = self._dump(client)
        assert self.SENTINEL not in dump
        assert all(item["status_message"] == "internal" for item in client.span_updates)
        assert client.generation_updates[0]["status_message"] == "internal"

    def test_safe_categories_pass_through(self, tmp_path):
        tracer, client = self._tracer(tmp_path)
        tracer.finish_turn(status="failed", totals={}, error="timeout")
        with tracer.tool(name="tool.x", metadata={}) as fields:
            fields.status_message = "tool_failed"
        assert client.span_updates[0]["status_message"] == "timeout"
        assert client.span_updates[1]["status_message"] == "tool_failed"

    def test_real_otel_db_span_does_not_record_exception_text(self, otel_exporter):
        """db_span 关闭 OTel 自动异常记录：地址 / 值正文不出现在导出事件里（OBS-02 #4）。"""
        from xhs_agent.core.tracing import db_span

        otel_exporter.clear()
        with (pytest.raises(RuntimeError),
              db_span("db.test", operation="insert", tables=("runs",))):
            raise RuntimeError(self.SENTINEL)
        (span,) = [item for item in otel_exporter.get_finished_spans() if item.name == "db.test"]
        events = [dict(event.attributes or {}) for event in (span.events or [])]
        assert self.SENTINEL not in json.dumps(
            {"attrs": dict(span.attributes or {}), "events": events,
             "status": str(span.status)}, ensure_ascii=False)
        assert not any("exception" in str(event) for event in events)

    @pytest.mark.asyncio
    async def test_provider_exception_and_error_result_project_text(self, tmp_path):
        tracer, client = self._tracer(tmp_path)
        provider = TracingProvider(FakeInnerProvider(error=RuntimeError(self.SENTINEL)),
                                   tracer, price_in_per_m=1.0, price_out_per_m=1.0)
        with pytest.raises(RuntimeError):
            await provider.complete(FakeCall())
        with pytest.raises(RuntimeError):
            await provider.complete_with_tools(FakeCall())
        assert [item["status_message"] for item in client.generation_updates] == \
            ["internal", "internal"]
        assert self.SENTINEL not in self._dump(client)

        provider2 = TracingProvider(FakeInnerProvider(result=FakeResult(error=self.SENTINEL)),
                                    tracer, price_in_per_m=1.0, price_out_per_m=1.0)
        await provider2.complete(FakeCall())
        assert client.generation_updates[2]["status_message"] == "llm_error"
        assert self.SENTINEL not in self._dump(client)

    def test_capture_true_still_projects_error_fields(self, tmp_path):
        """显式开启正文采集只放开 input / output；错误字段在任何开关下都只发安全类别。"""
        tracer, client = self._tracer(tmp_path, capture=True)
        tracer.finish_turn(status="failed", totals={}, error=self.SENTINEL)
        with tracer.generation(name="g", model="m", metadata={}) as fields:
            fields.input = {"system": "系统提示", "user": "用户输入"}
            fields.output = "正文"
            fields.status_message = self.SENTINEL
        assert client.span_updates[0]["status_message"] == "internal"
        update = client.generation_updates[0]
        assert update["status_message"] == "internal"
        assert update["input"]["user"] == "用户输入"      # 显式开关仍带正文
        assert self.SENTINEL not in update["input"]["user"]

    @pytest.mark.asyncio
    async def test_deltas_and_result_unchanged_when_sdk_creation_fails(self, tmp_path):
        """流式增量、返回值与追踪关闭时一致（业务只跑一次）。"""
        tracer, client = self._tracer(tmp_path)
        client.fail_create = True
        inner = FakeInnerProvider(result=FakeResult(text="正文",
                                                    tool_calls=[{"id": "1", "name": "x"}]))
        provider = TracingProvider(inner, tracer, price_in_per_m=1.0, price_out_per_m=1.0)
        deltas: list[str] = []

        async def on_delta(piece: str) -> None:
            deltas.append(piece)

        result = await provider.complete_with_tools(StreamCall(), on_delta=on_delta)
        assert result.text == "正文" and inner.delta_calls == 2
        assert deltas == ["增量", "文本"]
        assert client.generation_updates == []           # 观测没建起来：不写回
