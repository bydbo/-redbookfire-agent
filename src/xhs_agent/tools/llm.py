"""模型调用层。

用途：把「调一次聊天模型并拿到结构化 JSON」包成可替换的 provider，供上层编排使用。
输入：`LLMConfig`（provider / base_url / model / 密钥 / 超时 / 重试）+ `LLMCall`
      （task / system / user / json_mode）。
输出：`LLMResult`（text / provider / model / token / 耗时 / 成本 / error）。
      结构化输出统一走 `StructuredCaller`：解析失败时把错误回灌给模型自修一次。

- OpenAICompatibleProvider：任何兼容 /chat/completions 的国内模型都能接
  （DeepSeek、通义千问、智谱、Kimi、SiliconFlow…）
- OfflineProvider：无 Key 时接管（P2 取消运行时降级后删除，见 backlog S2.7）
- StructuredCaller：强制模型返回结构化 JSON，解析失败自动修复重试
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import AppConfig, LLMConfig
from ..util import extract_json
from . import offline


class LLMError(RuntimeError):
    """模型调用或结构化输出失败。"""


@dataclass
class LLMCall:
    task: str
    system: str
    user: str
    context: dict = field(default_factory=dict)
    json_mode: bool = True


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

    def record(self, task: str) -> dict:
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

    def complete(self, call: LLMCall) -> LLMResult:  # pragma: no cover - 接口定义
        raise NotImplementedError

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}" if self.model else self.name


class OpenAICompatibleProvider(BaseProvider):
    name = "openai_compatible"

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(model=cfg.model)
        self.cfg = cfg
        self.base_url = (cfg.base_url or "").rstrip("/")
        self.api_key = cfg.resolved_key()
        if not self.base_url:
            raise LLMError("缺少 base_url，无法调用在线模型")

    def complete(self, call: LLMCall) -> LLMResult:
        payload = {
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

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}/chat/completions"
        last_error = ""
        started = time.time()

        for attempt in range(1, self.cfg.max_retries + 2):
            request = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.cfg.timeout_s) as response:
                    data = json.loads(response.read().decode("utf-8", errors="replace"))
                text = _extract_message_text(data)
                usage = data.get("usage") or {}
                prompt_tokens = int(usage.get("prompt_tokens") or estimate_tokens(call.system + call.user))
                completion_tokens = int(usage.get("completion_tokens") or estimate_tokens(text))
                cost = (
                    prompt_tokens / 1_000_000 * self.cfg.price_in_per_m
                    + completion_tokens / 1_000_000 * self.cfg.price_out_per_m
                )
                return LLMResult(
                    text=text,
                    provider=self.name,
                    model=self.cfg.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=int((time.time() - started) * 1000),
                    cost_cny=cost,
                    attempts=attempt,
                )
            except urllib.error.HTTPError as exc:  # 4xx/5xx
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:300]
                except Exception:  # pragma: no cover
                    detail = ""
                last_error = f"HTTP {exc.code}: {detail or exc.reason}"
                if exc.code in (401, 403, 400):
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt <= self.cfg.max_retries:
                time.sleep(min(8.0, 1.5 ** attempt))

        return LLMResult(
            text="",
            provider=self.name,
            model=self.cfg.model,
            latency_ms=int((time.time() - started) * 1000),
            attempts=self.cfg.max_retries + 1,
            error=last_error or "调用失败",
        )


def _extract_message_text(data: dict) -> str:
    try:
        choices = data["choices"]
    except (KeyError, TypeError) as exc:
        raise LLMError(f"返回结构异常：{str(data)[:200]}") from exc
    if not choices:
        raise LLMError("返回 choices 为空")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        raise LLMError("返回内容为空")
    return str(content)


class OfflineProvider(BaseProvider):
    name = "offline"

    def __init__(self) -> None:
        super().__init__(model="rule-based")

    def complete(self, call: LLMCall) -> LLMResult:
        started = time.time()
        if call.task == "hotspot_clue":
            obj = offline.hotspot_clue(call.context.get("hotspot_raw", ""))
        elif call.task == "copy_draft":
            obj = offline.copy_draft(
                call.context.get("clue", {}),
                call.context.get("material", {}),
                call.context.get("style", ""),
            )
        elif call.task == "material_select":
            obj = {"selected_material_id": call.context.get("default_material_id", ""), "reasons": [], "usage": ""}
        else:
            raise LLMError(f"离线模式不支持的任务：{call.task}")
        text = json.dumps(obj, ensure_ascii=False)
        return LLMResult(
            text=text,
            provider=self.name,
            model="rule-based",
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=int((time.time() - started) * 1000),
        )


def build_provider(cfg: AppConfig, force_offline: bool = False) -> BaseProvider:
    provider_name = "offline" if force_offline else cfg.llm.resolved_provider()
    if provider_name == "offline":
        return OfflineProvider()
    if provider_name in {"openai_compatible", "openai", "deepseek", "qwen", "dashscope", "compatible"}:
        return OpenAICompatibleProvider(cfg.llm)
    raise LLMError(f"未知的 provider：{provider_name}")


@dataclass
class StructuredCaller:
    """把模型的自由文本压成结构化对象，失败时带错误信息让模型自修一次。"""

    provider: BaseProvider
    max_repairs: int = 1
    records: list = field(default_factory=list)

    def call(
        self,
        task: str,
        system: str,
        user: str,
        parse: Callable[[dict], object],
        context: dict | None = None,
        json_mode: bool = True,
    ):
        prompt = user
        last_error = ""
        for _attempt in range(self.max_repairs + 1):
            result = self.provider.complete(LLMCall(task=task, system=system, user=prompt,
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
