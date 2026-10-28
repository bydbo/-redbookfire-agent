"""真实提示词、模拟恶意上游响应：敏感片段不得进入回复、参数、SSE或异常。"""

import base64
import json
import re

import httpx
import pytest

from xhs_agent.config import EnvView, LLMConfig, VisionConfig
from xhs_agent.tools import prompt
from xhs_agent.tools.llm import (
    LLMCall,
    LLMError,
    LLMResult,
    OpenAICompatibleProvider,
    StructuredCaller,
)
from xhs_agent.tools.output_guard import UnsafeModelOutput, ensure_safe_output
from xhs_agent.tools.vision import AsyncImageClient, VisionError


def private_rule():
    return prompt.load("hotspot_clue").sections["constraints"]


@pytest.mark.parametrize("transform", [lambda value: value,
    lambda value: " · ".join(value), lambda value: json.dumps(value, ensure_ascii=True)])
def test_private_rules_are_blocked_even_with_spacing_or_unicode_escapes(transform):
    with pytest.raises(UnsafeModelOutput) as caught:
        ensure_safe_output(transform(private_rule()))
    assert private_rule() not in str(caught.value)


def test_public_reply_and_normal_clue_are_allowed():
    for category in prompt.load_capabilities()["categories"]:
        ensure_safe_output(category["reply"])
    ensure_safe_output('{"elements": [{"type":"scene", "value":"户外山野"}]}')
    rendered = prompt.render("chat_supervisor", capabilities=prompt.render_capabilities())
    example = prompt.load("chat_supervisor").sections["examples"].split("例 2", 1)[0]
    ensure_safe_output(example.split("- 你：", 1)[1], system=rendered.system)


def test_tool_arguments_and_custom_system_rules_are_checked():
    with pytest.raises(UnsafeModelOutput):
        ensure_safe_output("好的", tool_calls=[{"arguments": json.dumps({"query": private_rule()})}])
    secret = "private-server-instruction-marker-" * 5
    with pytest.raises(UnsafeModelOutput):
        ensure_safe_output(secret, system=secret)


def test_nested_json_newlines_and_base64_cannot_bypass_check():
    secret = "alphaabcdefghijklmnopqrstuvw\nbetaxyzabcdefghijklmnopqrst\ngammaabcdefghijklmnopqrstuv"
    for text in (json.dumps({"body": secret}), base64.b64encode(secret.encode()).decode()):
        with pytest.raises(UnsafeModelOutput):
            ensure_safe_output(text, system=secret)
    with pytest.raises(UnsafeModelOutput):
        ensure_safe_output("", system=secret,
            tool_calls=[{"arguments": json.dumps({"body": secret})}])


def test_complete_fenced_prompt_example_is_blocked():
    examples = prompt.load("hotspot_clue").sections["examples"]
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", examples, re.DOTALL)
    assert blocks
    with pytest.raises(UnsafeModelOutput):
        ensure_safe_output(blocks[-1])


def provider(client):
    cfg = LLMConfig(base_url="https://model.invalid/v1", model="test", max_retries=0)
    cfg._env = EnvView({}, {})
    return OpenAICompatibleProvider(cfg, client=client)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_response_and_split_stream_never_release_private_text(stream):
    secret = private_rule()
    pieces = [secret[i:i + 7] for i in range(0, len(secret), 7)]
    chunks = [{"choices": [{"delta": {"content": piece}}]} for piece in pieces]
    chunks.append({"choices": [], "usage": {"prompt_tokens": 80, "completion_tokens": 90}})
    body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    def handler(_request):
        if stream:
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": secret}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 90}})
    released = []
    async def delta(piece):
        released.append(piece)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await provider(client).complete_with_tools(
            LLMCall(task="chat_supervisor", system="s", user="u", stream=stream), on_delta=delta)
    assert result.error and result.text == "" and result.tool_calls == [] and released == []
    assert result.completion_tokens == 90 and result.usage_status == "actual"
    assert result.attempts_detail[-1]["status"] == "failed"
    assert secret not in json.dumps(result.record("chat_supervisor"))


@pytest.mark.asyncio
async def test_tool_argument_exfiltration_does_not_dispatch():
    def handler(_request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "",
            "tool_calls": [{"id": "1", "function": {"name": "search_materials",
                "arguments": json.dumps({"query": private_rule()})}}]}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await provider(client).complete(LLMCall(task="chat_supervisor", system="s", user="u"))
    assert result.error and result.tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 500])
@pytest.mark.parametrize("stream", [False, True])
async def test_upstream_error_body_is_not_returned_or_recorded(status, stream):
    marker = "private request echoed by upstream"
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, text=marker))) as client:
        result = await provider(client).complete(LLMCall(task="hotspot_clue", system="s",
                                                       user="u", stream=stream))
    assert str(status) in result.error
    assert marker not in json.dumps(result.record("hotspot_clue"))


@pytest.mark.asyncio
async def test_parser_error_does_not_echo_raw_output_into_repair_or_error():
    marker = "confidential-output-marker"
    class Fake:
        calls = []
        async def complete(self, call):
            self.calls.append(call)
            return LLMResult(text='{"value": 3}')
    fake = Fake()
    caller = StructuredCaller(provider=fake)
    def parse(_payload):
        raise ValueError(marker)
    with pytest.raises(LLMError) as caught:
        await caller.call("test", "s", "u", parse)
    assert marker not in str(caught.value)
    assert marker not in fake.calls[-1].user


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["echo", "http", "bad_json", "bad_shape"])
async def test_vision_output_and_error_paths_do_not_return_internal_text(response_kind):
    secret = private_rule()
    responses = {"echo": httpx.Response(200, json={"choices": [{"message": {"content": secret}}],
                     "usage": {"prompt_tokens": 100, "completion_tokens": 200}}),
        "http": httpx.Response(500, text=secret), "bad_json": httpx.Response(200, text=secret),
        "bad_shape": httpx.Response(200, json={"echo": secret})}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: responses[response_kind])) as client:
        cfg = VisionConfig(base_url="https://model.invalid/v1")
        cfg._env = EnvView({}, {})
        vision = AsyncImageClient(cfg, client=client)
        with pytest.raises(VisionError) as caught:
            await vision.complete(prompt.render("image_hotspot_clue"), b"img", "image/png")
    assert secret not in str(caught.value)
    if response_kind == "echo":
        assert caught.value.completion.text == ""
        assert caught.value.completion.completion_tokens == 200
        assert caught.value.completion.usage_status == "actual"
