"""模型调用层。

用途：把「调一次聊天模型并拿到结构化 JSON」包成可替换的 provider，供上层编排使用。
输入：`LLMConfig`（provider / base_url / model / 密钥 / 超时 / 重试）+ `LLMCall`
      （task / system / user / json_mode）+ 调用方注入的 `httpx.AsyncClient`（连接池）。
输出：`LLMResult`（text / provider / model / token / 耗时 / 成本 / error / tool_calls）。
      结构化输出统一走 `StructuredCaller`：解析失败时把错误回灌给模型自修一次；
      对话层（S7.1）走 `complete_with_tools`：原生 function calling + 可选真 token 流式。

- OpenAICompatibleProvider：任何兼容 /chat/completions 的国内模型都能接
  （DeepSeek、通义千问、智谱、Kimi、SiliconFlow…）
- StructuredCaller：强制模型返回结构化 JSON，解析失败自动修复重试
- `complete_with_tools`：带 `tools` 的调用（`tool_choice=auto`），`stream=True` 时逐分片回吐正文

传输层用 httpx 异步客户端（S3.5）：客户端由调用方按「一次运行一个」注入，本模块不自持；
重试口径不变（`max_retries + 1` 次、退避见 `tools/http.py`、400/401/403 不重试）。
运行时降级已取消（ADR 0001）：没有密钥就是配置错误，启动前置检查会拦住，不存在"离线接管"。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx

from ..config import LLM_PROVIDERS, AppConfig, LLMConfig
from ..util import extract_json
from .http import backoff_seconds

T = TypeVar("T")


class LLMError(RuntimeError):
    """模型调用或结构化输出失败。"""


@dataclass
class LLMCall:
    task: str
    system: str
    user: str
    context: dict[str, Any] = field(default_factory=dict)
    json_mode: bool = True
    # S7.1：对话层用原生 function calling——`tools` 是 OpenAI 兼容的函数清单，
    # `stream=True` 时增量回吐正文（走 `complete_with_tools(on_delta=...)`）。
    tools: list[dict[str, Any]] | None = None
    stream: bool = False


@dataclass
class LLMResult:
    text: str
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_cny: float = 0.0
    attempts: int = 1
    error: str = ""
    # S7.1：模型要求调用的工具（`{id, name, arguments}`，arguments 是原始 JSON 字符串）
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, task: str) -> dict[str, Any]:
        return {
            "task": task,
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": self.latency_ms,
            "cost_cny": round(self.cost_cny, 4),
            "attempts": self.attempts,
            "error": self.error,
        }


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文约 1.5 字/token）。"""
    return max(1, int(len(str(text or "")) / 1.5))


class BaseProvider:
    name = "base"

    def __init__(self, model: str = "") -> None:
        self.model = model

    async def complete(self, call: LLMCall) -> LLMResult:  # pragma: no cover - 接口定义
        raise NotImplementedError

    async def complete_with_tools(
        self, call: LLMCall,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResult:  # pragma: no cover - 接口定义
        """带工具调用（可选流式）的一次调用（S7.1）。

        `on_delta` 只在 `call.stream=True` 时被逐段调用（真 token 增量）；返回的
        `LLMResult.tool_calls` 是"模型要求调用的工具"清单，为空表示这一轮是普通回答。
        不支持的实现直接抛 `NotImplementedError`——不做静默降级。
        """
        raise NotImplementedError

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}" if self.model else self.name


class OpenAICompatibleProvider(BaseProvider):
    name = "openai_compatible"

    def __init__(self, cfg: LLMConfig, *, client: httpx.AsyncClient) -> None:
        super().__init__(model=cfg.model)
        self.cfg = cfg
        self.base_url = (cfg.base_url or "").rstrip("/")
        self.api_key = cfg.resolved_key()
        self.client = client
        if not self.base_url:
            raise LLMError("缺少 base_url，无法调用在线模型")

    def _payload(self, call: LLMCall) -> dict[str, Any]:
        """组请求体：`tools` / `stream` 只在显式给了才带（S7.1）。

        普通结构化调用（不带工具、不流式）的请求体与 S7.1 之前**逐字段一致**。
        """
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": call.system},
                {"role": "user", "content": call.user},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if call.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if call.tools:
            payload["tools"] = [dict(tool) for tool in call.tools]
            payload["tool_choice"] = "auto"
        if call.stream:
            # 不带 stream_options：不是所有兼容端点都认它；usage 缺失时按字符估算（见 `_result`）
            payload["stream"] = True
        return payload

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def _result(self, call: LLMCall, *, text: str, tool_calls: list[dict[str, Any]],
                usage: dict[str, Any] | None, attempt: int, started: float,
                error: str = "") -> LLMResult:
        """统一记账口径：token 取 `usage`，缺失时按字符估算（与 S7.1 之前一致）。"""
        prompt_tokens = int((usage or {}).get("prompt_tokens")
                            or estimate_tokens(call.system + call.user))
        completion_tokens = int((usage or {}).get("completion_tokens")
                                or estimate_tokens(text))
        cost = (prompt_tokens / 1_000_000 * self.cfg.price_in_per_m
                + completion_tokens / 1_000_000 * self.cfg.price_out_per_m)
        return LLMResult(
            text=text,
            provider=self.name,
            model=self.cfg.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=int((time.time() - started) * 1000),
            cost_cny=cost,
            attempts=attempt,
            error=error,
            tool_calls=list(tool_calls),
        )

    async def complete(self, call: LLMCall) -> LLMResult:
        """一次调用（不带工具、不流式）。与 `complete_with_tools` 共用同一实现路径。"""
        return await self.complete_with_tools(call)

    async def complete_with_tools(
        self, call: LLMCall,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResult:
        """带工具调用的一次调用，可选真 token 流式（S7.1）。

        重试口径与原来一致（`max_retries + 1` 次、退避见 `tools/http.py`、400/401/403 不重试）。
        唯一的例外：**流式已经开始回吐正文就不再重试**——重试会让下游（和用户）看到重复文本，
        此时把已吐出的半截文本连同 `error` 一起返回，由调用方决定怎么收尾。
        """
        payload = self._payload(call)
        url = f"{self.base_url}/chat/completions"
        headers = self._headers()
        last_error = ""
        started = time.time()

        for attempt in range(1, self.cfg.max_retries + 2):
            sink: dict[str, Any] = {"text": ""}
            try:
                if call.stream:
                    text, tool_calls, usage = await self._consume_stream(
                        url, payload, headers, on_delta, sink)
                    return self._result(call, text=text, tool_calls=tool_calls, usage=usage,
                                        attempt=attempt, started=started)
                response = await self.client.post(url, json=payload, headers=headers,
                                                  timeout=self.cfg.timeout_s)
                if response.status_code < 400:
                    data = response.json()
                    message = _first_message(data)
                    return self._result(call, text=_content_text(message.get("content")),
                                        tool_calls=_tool_calls_of(message),
                                        usage=data.get("usage") or {},
                                        attempt=attempt, started=started)
                detail = response.text[:300]
                last_error = (f"HTTP {response.status_code}: "
                              f"{detail or response.reason_phrase}")
                if response.status_code in (401, 403, 400):
                    break
            except _StreamHTTPError as exc:      # 流式响应的 4xx/5xx
                last_error = str(exc)
                if exc.status in (401, 403, 400):
                    break
                if sink["text"]:
                    return self._result(call, text=sink["text"], tool_calls=[], usage=None,
                                        attempt=attempt, started=started, error=last_error)
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if sink["text"]:
                    return self._result(call, text=sink["text"], tool_calls=[], usage=None,
                                        attempt=attempt, started=started, error=last_error)
            if attempt <= self.cfg.max_retries:
                await asyncio.sleep(backoff_seconds(attempt))

        return self._result(call, text="", tool_calls=[], usage=None,
                            attempt=self.cfg.max_retries + 1, started=started,
                            error=last_error or "调用失败")

    async def _consume_stream(
        self, url: str, payload: dict[str, Any], headers: dict[str, str],
        on_delta: Callable[[str], Awaitable[None]] | None, sink: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
        """读 `text/event-stream`：正文增量回调 `on_delta`，工具调用增量按 `index` 拼接。

        返回 `(正文, tool_calls, usage)`；`usage` 只有供应商在分片里给了才有。
        """
        text_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] | None = None
        async with self.client.stream("POST", url, json=payload, headers=headers,
                                      timeout=self.cfg.timeout_s) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")[:300]
                raise _StreamHTTPError(response.status_code,
                                       f"HTTP {response.status_code}: "
                                       f"{body or response.reason_phrase}")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if not body:
                    continue
                if body == "[DONE]":
                    break
                chunk = json.loads(body)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = _content_text(delta.get("content"))
                if piece:
                    text_parts.append(piece)
                    sink["text"] = "".join(text_parts)
                    if on_delta is not None:
                        await on_delta(piece)
                for raw in delta.get("tool_calls") or []:
                    index = int(raw.get("index") or 0)
                    slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if raw.get("id"):
                        slot["id"] = str(raw["id"])
                    function = raw.get("function") or {}
                    if function.get("name"):
                        slot["name"] = str(function["name"])
                    if function.get("arguments"):
                        slot["arguments"] += str(function["arguments"])
        return ("".join(text_parts),
                [calls[key] for key in sorted(calls) if calls[key]["name"]],
                usage)


def _first_message(data: dict[str, Any]) -> dict[str, Any]:
    """取 `choices[0].message`；结构异常时抛 `LLMError`（与 S7.1 之前同一套错误）。"""
    try:
        choices = data["choices"]
    except (KeyError, TypeError) as exc:
        raise LLMError(f"返回结构异常：{str(data)[:200]}") from exc
    if not choices:
        raise LLMError("返回 choices 为空")
    message = choices[0].get("message")
    return message if isinstance(message, dict) else {}


def _content_text(content: Any) -> str:
    """`content` 既可能是字符串，也可能是 `[{"type": "text", "text": ...}]` 数组。"""
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content
                       if isinstance(part, dict))
    return "" if content is None else str(content)


def _tool_calls_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    """把响应里的 `tool_calls` 归一成 `[{id, name, arguments}]`（S7.1）。"""
    out: list[dict[str, Any]] = []
    for raw in message.get("tool_calls") or []:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        name = str(function.get("name") or "")
        if not name:
            continue
        out.append({"id": str(raw.get("id") or ""), "name": name,
                    "arguments": str(function.get("arguments") or "")})
    return out


class _StreamHTTPError(RuntimeError):
    """流式响应的 HTTP 错误（带状态码，交给重试循环决定重试/终止）。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _extract_message_text(data: dict[str, Any]) -> str:
    """取回复正文；缺 choices 或正文为空都抛 `LLMError`（结构化调用依赖这个口径）。"""
    content = _content_text(_first_message(data).get("content"))
    if not content:
        raise LLMError("返回内容为空")
    return content


def build_provider(cfg: AppConfig, *, client: httpx.AsyncClient) -> BaseProvider:
    """按配置造 provider：只支持 OpenAI 兼容端点，未知值抛 `LLMError`（无效配置在启动前置检查就拦）。"""
    provider_name = cfg.llm.resolved_provider()
    # 白名单与配置层共用一份（config.LLM_PROVIDERS），避免两处漂移
    if provider_name in LLM_PROVIDERS:
        return OpenAICompatibleProvider(cfg.llm, client=client)
    raise LLMError(f"未知的 provider：{provider_name}")


@dataclass
class StructuredCaller:
    """把模型的自由文本压成结构化对象，失败时带错误信息让模型自修一次。"""

    provider: BaseProvider
    max_repairs: int = 1
    records: list[dict[str, Any]] = field(default_factory=list)

    async def call(
        self,
        task: str,
        system: str,
        user: str,
        parse: Callable[[Any], T],
        context: dict[str, Any] | None = None,
        json_mode: bool = True,
    ) -> tuple[T, LLMResult]:
        prompt = user
        last_error = ""
        for _attempt in range(self.max_repairs + 1):
            result = await self.provider.complete(
                LLMCall(task=task, system=system, user=prompt,
                        context=context or {}, json_mode=json_mode))
            self.records.append(result.record(task))
            if result.error:
                last_error = result.error
                break
            try:
                payload = extract_json(result.text)
                return parse(payload), result
            except Exception as exc:  # SchemaError / ValueError
                last_error = f"{type(exc).__name__}: {exc}"
                prompt = (
                    f"{user}\n\n"
                    f"上一次输出无法被解析，错误是：{last_error}。\n"
                    "请只输出修正后的 JSON，不要解释、不要代码块以外的任何文字。"
                )
        raise LLMError(f"[{task}] 结构化输出失败：{last_error}")
