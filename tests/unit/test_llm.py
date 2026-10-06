"""模型调用层单测：全部用假 provider / `httpx.MockTransport`，不联网、不读密钥。"""

from __future__ import annotations

import importlib
import inspect
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import pytest_asyncio

from xhs_agent.config import EnvView, LLMConfig
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMError, LLMResult, StructuredCaller

Handler = Callable[[dict[str, Any], int], httpx.Response]


class MockAPI:
    """用 `httpx.MockTransport` 顶替真实端点：记录每次请求，handler 决定响应。"""

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
    """按用例给的 handler 造 MockAPI，用例结束统一关闭客户端。"""
    created: list[MockAPI] = []

    def _make(handler: Handler) -> MockAPI:
        api = MockAPI(handler)
        created.append(api)
        return api

    yield _make
    for api in created:
        await api.aclose()


@pytest_asyncio.fixture
async def idle_client() -> AsyncIterator[httpx.AsyncClient]:
    """只用于构造 provider 的空客户端；一旦真被使用就报错。"""
    def no_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("本用例不应发起任何请求")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_request)) as client:
        yield client


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试退避在测试里立即返回，避免用例真的等 1.5s / 3s。"""
    monkeypatch.setattr(llm, "backoff_seconds", lambda _attempt: 0.0)


def chat_response(text: str = '{"value": 7}', prompt_tokens: int = 100,
                  completion_tokens: int = 50) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    })


class FakeProvider(llm.BaseProvider):
    """按预设文本依次返回，用来观察重试行为。"""

    name = "fake"

    def __init__(self, texts):
        super().__init__(model="fake-1")
        self.texts = list(texts)
        self.prompts = []

    async def complete(self, call):
        self.prompts.append(call.user)
        text = self.texts.pop(0) if self.texts else ""
        return LLMResult(text=text, provider=self.name, model=self.model)


class TestEstimateTokens:
    def test_empty_text_has_floor_of_one(self):
        assert llm.estimate_tokens("") == 1

    def test_roughly_one_and_a_half_chars_per_token(self):
        assert llm.estimate_tokens("abcde") == 3
        assert llm.estimate_tokens("a" * 150) == 100


class TestLLMResult:
    def test_record_shape(self):
        result = LLMResult(text="x", provider="fake", model="m", prompt_tokens=10,
                           completion_tokens=5, latency_ms=100, cost_cny=0.00123)
        record = result.record("hotspot_clue")
        assert record == {
            "task": "hotspot_clue",
            "provider": "fake",
            "model": "m",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "latency_ms": 100,
            "cost_cny": 0.0012,
            "attempts": 1,
            "error": "",
        }

    def test_label_uses_model_name(self):
        assert FakeProvider(["{}"]).label == "fake:fake-1"


class TestStructuredCaller:
    @pytest.mark.asyncio
    async def test_parses_json_on_first_try(self):
        caller = StructuredCaller(provider=FakeProvider(['{"value": 7}']))
        parsed, result = await caller.call("task", "system", "user",
                                           parse=lambda data: data["value"])
        assert parsed == 7
        assert result.text == '{"value": 7}'
        assert len(caller.records) == 1

    @pytest.mark.asyncio
    async def test_repairs_by_feeding_error_back(self):
        provider = FakeProvider(["不是 JSON", '{"value": 7}'])
        caller = StructuredCaller(provider=provider)
        parsed, _result = await caller.call("task", "system", "user",
                                            parse=lambda data: data["value"])
        assert parsed == 7
        assert len(provider.prompts) == 2
        assert "上一次输出无法被解析" in provider.prompts[1]

    @pytest.mark.asyncio
    async def test_raises_after_repair_budget_is_used_up(self):
        caller = StructuredCaller(provider=FakeProvider(["坏输出", "还是坏输出"]))
        with pytest.raises(LLMError):
            await caller.call("task", "system", "user", parse=lambda data: data["value"])

    @pytest.mark.asyncio
    async def test_provider_error_is_surfaced(self):
        class FailingProvider(FakeProvider):
            async def complete(self, call):
                return LLMResult(text="", provider=self.name, error="连接失败")

        caller = StructuredCaller(provider=FailingProvider([]))
        with pytest.raises(LLMError) as info:
            await caller.call("task", "system", "user", parse=lambda data: data)
        assert "连接失败" in str(info.value)


class TestOpenAICompatibleProvider:
    """S3.5：模型调用走注入的 httpx 客户端，请求体与重试口径与旧实现一致。"""

    @pytest.mark.asyncio
    async def test_sends_payload_and_reads_usage(self, make_api):
        api = make_api(lambda _payload, _n: chat_response())
        provider = llm.build_provider(FakeConfig(), client=api.client)
        result = await provider.complete(llm.LLMCall(task="hotspot_clue", system="系统",
                                                     user="用户"))

        call = api.calls[0]
        assert call["url"] == "https://example.test/v1/chat/completions"
        assert call["timeout"] == 7
        assert call["authorization"] == "Bearer sk-test"
        assert call["payload"]["model"] == "deepseek-flash"
        assert call["payload"]["messages"] == [{"role": "system", "content": "系统"},
                                               {"role": "user", "content": "用户"}]
        assert call["payload"]["response_format"] == {"type": "json_object"}
        assert call["payload"]["temperature"] == 0.6
        assert call["payload"]["max_tokens"] == 2000

        assert result.text == '{"value": 7}'
        assert (result.prompt_tokens, result.completion_tokens) == (100, 50)
        assert result.cost_cny == pytest.approx(100 / 1e6 * 2.0 + 50 / 1e6 * 8.0)
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_json_mode_off_omits_response_format(self, make_api):
        api = make_api(lambda _payload, _n: chat_response())
        provider = llm.build_provider(FakeConfig(), client=api.client)
        await provider.complete(llm.LLMCall(task="t", system="s", user="u", json_mode=False))
        assert "response_format" not in api.calls[0]["payload"]

    @pytest.mark.asyncio
    async def test_retries_transient_then_succeeds(self, make_api, no_backoff):
        def handler(_payload, n):
            if n == 1:
                raise httpx.ConnectError("boom")
            return chat_response()

        api = make_api(handler)
        provider = llm.build_provider(FakeConfig(), client=api.client)
        result = await provider.complete(llm.LLMCall(task="t", system="s", user="u"))
        assert len(api.calls) == 2
        assert result.attempts == 2
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_all_attempts_fail_returns_error_result(self, make_api, no_backoff):
        def handler(_payload, _n):
            raise httpx.ConnectError("down")

        api = make_api(handler)
        provider = llm.build_provider(FakeConfig(), client=api.client)
        result = await provider.complete(llm.LLMCall(task="t", system="s", user="u"))
        assert len(api.calls) == 3          # 1 次 + max_retries=2
        assert "ConnectError" in result.error
        assert result.text == ""

    @pytest.mark.asyncio
    async def test_401_is_not_retried(self, make_api, no_backoff):
        api = make_api(lambda _payload, _n: httpx.Response(401, json={"error": "bad key"}))
        provider = llm.build_provider(FakeConfig(), client=api.client)
        result = await provider.complete(llm.LLMCall(task="t", system="s", user="u"))
        assert len(api.calls) == 1
        assert "HTTP 401" in result.error


class TestBuildProvider:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["openai_compatible", "openai", "deepseek", "qwen",
                                      "dashscope", "compatible"])
    async def test_compatible_aliases_share_one_implementation(self, name, idle_client):
        provider = llm.build_provider(FakeConfig(provider=name), client=idle_client)
        assert isinstance(provider, llm.OpenAICompatibleProvider)

    @pytest.mark.asyncio
    async def test_unknown_provider_is_rejected(self, idle_client):
        # provider 的合法值由配置层校验（S1.2），这里用 model_construct 绕过校验，
        # 验证工具层自己的兜底分支仍然存在。
        with pytest.raises(LLMError):
            llm.build_provider(FakeConfig(provider="bogus"), client=idle_client)

    @pytest.mark.asyncio
    async def test_empty_base_url_is_rejected(self, idle_client):
        with pytest.raises(LLMError):
            llm.build_provider(FakeConfig(base_url=""), client=idle_client)

    def test_client_is_a_required_keyword_argument(self):
        """S3.5：客户端由调用方按「一次运行一个」注入，provider 不自持。"""
        parameter = inspect.signature(llm.build_provider).parameters["client"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


class TestOfflinePathIsGone:
    """S2.7：运行时降级链路已整条删除，这里把"不许复活"钉成可回归的断言。"""

    def test_offline_module_is_removed(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("xhs_agent.tools.offline")

    def test_offline_provider_class_is_removed(self):
        assert not hasattr(llm, "OfflineProvider")

    def test_build_provider_has_no_force_offline_parameter(self):
        assert "force_offline" not in inspect.signature(llm.build_provider).parameters

    @pytest.mark.asyncio
    async def test_offline_provider_name_is_rejected(self, idle_client):
        with pytest.raises(LLMError):
            llm.build_provider(FakeConfig(provider="offline"), client=idle_client)


class TestExtractMessageText:
    def test_plain_content(self):
        assert llm._extract_message_text({"choices": [{"message": {"content": "你好"}}]}) == "你好"

    def test_list_content_is_joined(self):
        payload = {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}
        assert llm._extract_message_text(payload) == "ab"

    def test_missing_choices_raises(self):
        with pytest.raises(LLMError):
            llm._extract_message_text({})

    def test_empty_content_raises(self):
        with pytest.raises(LLMError):
            llm._extract_message_text({"choices": [{"message": {"content": ""}}]})


class FakeConfig:
    """只需要 provider / base_url / model 等字段，避免测试依赖真实密钥。"""

    def __init__(self, **llm_overrides) -> None:
        # model_construct 跳过配置层校验，用来单独验证工具层的兜底分支
        fields = {"provider": "openai_compatible", "base_url": "https://example.test/v1",
                  "model": "deepseek-flash", "api_key_env": "DEEPSEEK_API_KEY",
                  "temperature": 0.6, "max_tokens": 2000, "timeout_s": 7, "max_retries": 2,
                  "price_in_per_m": 2.0, "price_out_per_m": 8.0}
        fields.update(llm_overrides)
        self.llm = LLMConfig.model_construct(**fields)
        # 假密钥：httpx 会校验请求头，空 key 的 "Bearer " 在真实连接上会被拒（LocalProtocolError）
        self.llm._env = EnvView({}, {"DEEPSEEK_API_KEY": "sk-test"})


# ---------- S7.1 原生 function calling 与真 token 流式 ----------

def tool_calls_response(name: str = "run_hotspot_analysis",
                        arguments: str = '{"hotspot": "x"}',
                        call_id: str = "call_1") -> httpx.Response:
    """非流式：模型要求调用工具（正文为空、finish_reason=tool_calls）。"""
    return httpx.Response(200, json={
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {"content": "", "tool_calls": [
                {"id": call_id, "type": "function",
                 "function": {"name": name, "arguments": arguments}},
            ]},
        }],
        "usage": {"prompt_tokens": 200, "completion_tokens": 30},
    })


def text_delta(text: str) -> dict[str, Any]:
    return {"choices": [{"delta": {"content": text}}]}


def tool_delta(index: int = 0, *, call_id: str | None = None, name: str | None = None,
               arguments: str | None = None) -> dict[str, Any]:
    function: dict[str, Any] = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    raw: dict[str, Any] = {"index": index, "function": function}
    if call_id is not None:
        raw["id"] = call_id
    return {"choices": [{"delta": {"tool_calls": [raw]}}]}


def sse_body(chunks: list[dict[str, Any]]) -> bytes:
    lines = [f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks]
    return ("".join(lines) + "data: [DONE]\n\n").encode("utf-8")


def sse_response(chunks: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, content=sse_body(chunks),
                          headers={"content-type": "text/event-stream"})


class TestToolCalling:
    """S7.1：`tools` / `tool_calls` 与流式聚合——请求形状、记账与失败口径。"""

    @pytest.mark.asyncio
    async def test_payload_carries_tools_and_auto_choice(self, make_api):
        api = make_api(lambda _payload, _n: tool_calls_response())
        provider = llm.build_provider(FakeConfig(), client=api.client)
        tools = [{"type": "function", "function": {"name": "run_hotspot_analysis",
                                                   "description": "跑完整分析",
                                                   "parameters": {"type": "object"}}}]

        result = await provider.complete_with_tools(
            llm.LLMCall(task="chat_supervisor", system="s", user="u", json_mode=False,
                        tools=tools))

        payload = api.calls[0]["payload"]
        assert payload["tools"] == tools and payload["tool_choice"] == "auto"
        assert "stream" not in payload
        assert result.text == ""
        assert result.tool_calls == [{"id": "call_1", "name": "run_hotspot_analysis",
                                      "arguments": '{"hotspot": "x"}'}]
        assert (result.prompt_tokens, result.completion_tokens) == (200, 30)
        assert result.cost_cny == pytest.approx(200 / 1e6 * 2.0 + 30 / 1e6 * 8.0)
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_plain_complete_does_not_add_tool_fields(self, make_api):
        api = make_api(lambda _payload, _n: chat_response())
        provider = llm.build_provider(FakeConfig(), client=api.client)
        await provider.complete(llm.LLMCall(task="t", system="s", user="u"))
        payload = api.calls[0]["payload"]
        assert "tools" not in payload and "tool_choice" not in payload
        assert "stream" not in payload
        assert payload["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_streaming_emits_deltas_and_accumulates_text(self, make_api):
        chunks = [text_delta("你好"), text_delta("，"), text_delta("世界"),
                  {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}}]
        api = make_api(lambda _payload, _n: sse_response(chunks))
        provider = llm.build_provider(FakeConfig(), client=api.client)
        seen: list[str] = []

        async def on_delta(piece: str) -> None:
            seen.append(piece)

        result = await provider.complete_with_tools(
            llm.LLMCall(task="chat_supervisor", system="s", user="u", json_mode=False,
                        stream=True),
            on_delta=on_delta)

        assert api.calls[0]["payload"]["stream"] is True
        assert seen == ["你好", "，", "世界"]
        assert result.text == "你好，世界"
        assert result.tool_calls == []
        assert (result.prompt_tokens, result.completion_tokens) == (11, 7)
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_streaming_accumulates_tool_call_arguments(self, make_api):
        chunks = [tool_delta(call_id="call_9", name="run_hotspot_analysis",
                             arguments='{"hot'),
                  tool_delta(arguments='spot": "夜跑"}')]
        api = make_api(lambda _payload, _n: sse_response(chunks))
        provider = llm.build_provider(FakeConfig(), client=api.client)

        result = await provider.complete_with_tools(
            llm.LLMCall(task="chat_supervisor", system="s", user="u", json_mode=False,
                        tools=[{"type": "function"}], stream=True))

        assert result.text == ""
        assert result.tool_calls == [{"id": "call_9", "name": "run_hotspot_analysis",
                                      "arguments": '{"hotspot": "夜跑"}'}]

    @pytest.mark.asyncio
    async def test_streaming_without_usage_estimates_tokens(self, make_api):
        api = make_api(lambda _payload, _n: sse_response([text_delta("一二三四五六")]))
        provider = llm.build_provider(FakeConfig(), client=api.client)
        result = await provider.complete_with_tools(
            llm.LLMCall(task="chat_supervisor", system="s", user="u", json_mode=False,
                        stream=True))
        assert result.text == "一二三四五六"
        assert result.prompt_tokens > 0 and result.completion_tokens > 0
        assert result.cost_cny > 0

    @pytest.mark.asyncio
    async def test_stream_400_is_not_retried(self, make_api, no_backoff):
        api = make_api(lambda _payload, _n: httpx.Response(400, json={"error": "bad"}))
        provider = llm.build_provider(FakeConfig(), client=api.client)

        result = await provider.complete_with_tools(
            llm.LLMCall(task="chat_supervisor", system="s", user="u", json_mode=False,
                        stream=True))

        assert len(api.calls) == 1
        assert "HTTP 400" in result.error and result.text == ""

    @pytest.mark.asyncio
    async def test_stream_failure_after_first_delta_does_not_retry(self, make_api, no_backoff):
        body = sse_body([text_delta("前半")]).replace(
            b"data: [DONE]\n\n", b"data: not-json\n\ndata: [DONE]\n\n")
        api = make_api(lambda _payload, _n: httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}))
        provider = llm.build_provider(FakeConfig(), client=api.client)
        seen: list[str] = []

        async def on_delta(piece: str) -> None:
            seen.append(piece)

        result = await provider.complete_with_tools(
            llm.LLMCall(task="chat_supervisor", system="s", user="u", json_mode=False,
                        stream=True),
            on_delta=on_delta)

        assert seen == ["前半"]            # 已经吐出去的字不重试、不重复
        assert result.text == "前半"
        assert result.error != ""
        assert result.attempts == 1
        assert len(api.calls) == 1
