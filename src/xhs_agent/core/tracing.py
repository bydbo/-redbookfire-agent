"""调用追踪门面：把一次分析上报到 Langfuse（S4.2）。

用途：`get_tracer(cfg)` 拿进程级追踪器（缺键时是 `NullTracer`）；`run_analysis` 用它开
      「运行 → 热点 → 模型调用」三层结构，`TracingProvider` 负责每次文本模型调用的 generation。
输入：`AppConfig`——Langfuse 三件套从环境变量 / `config/.env` 读（《配置契约》§2.2）；
      正文开关读 `LANGFUSE_CAPTURE_CONTENT`。
输出：无返回值（上报是副作用）；`availability()` / `capture_content()` 供启动时打印口径。

口径（改动要同步 `docs/开发规范.md` 与《配置契约》§2.2）：

- **旁路组件**：三件套不齐只打一条 warning 并关闭追踪，绝不阻断分析链路；上报失败只记日志。
- **默认只上报元数据**（模型名 / task / run_id / token / 成本 / 延迟 / 错误状态），
  `LANGFUSE_CAPTURE_CONTENT=true` 时才带上提示词与产出正文——这是 ADR 0005 的
  "不默认上报素材路径与提示词原文，需要时显式开启"。
- **进程级懒加载单例**：Langfuse SDK 初始化时会调全局
  `opentelemetry.trace.set_tracer_provider()`（进程内只能设一次），所以客户端不能每次运行新建；
  懒建同时保证 Celery prefork 时客户端只在真正干活的子进程里创建，不跨 fork 继承。
- **业务代码不直接 `import langfuse`**：一律走本模块的 `Tracer` 协议，离线测试注入假实现。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import AppConfig

logger = logging.getLogger("xhs_agent.tracing")

# 运行 trace 的名字（Langfuse UI 里按这个名字过滤）
TRACE_NAME = "analyze"
# `LANGFUSE_CAPTURE_CONTENT` 的真值写法（大小写不敏感）
TRUTHY: tuple[str, ...] = ("1", "true", "yes", "on")
LEVEL_DEFAULT = "DEFAULT"
LEVEL_ERROR = "ERROR"


def availability(cfg: AppConfig) -> tuple[bool, str]:
    """追踪是否可用：三件套齐全才算启用；否则返回一句可照做的原因。"""
    public, secret, host = cfg.langfuse_keys
    missing = [name for name, value in (("LANGFUSE_PUBLIC_KEY", public),
                                        ("LANGFUSE_SECRET_KEY", secret),
                                        ("LANGFUSE_HOST", host)) if not value]
    if missing:
        return False, ("缺少 " + " / ".join(missing)
                       + "（三个都写进 config/.env 才启用；追踪是旁路组件，关闭不影响分析）")
    return True, ""


def capture_content(cfg: AppConfig) -> bool:
    """是否上报提示词与产出正文（默认 false，符合 ADR 0005）。"""
    return str(cfg.env_view.get("LANGFUSE_CAPTURE_CONTENT") or "").strip().lower() in TRUTHY


@dataclass
class Generation:
    """一次模型调用的上报字段：调用方在 `with tracer.generation(...)` 块内填，退出时上报。"""

    usage_details: dict[str, int] = field(default_factory=dict)
    cost_details: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    input: Any = None
    output: Any = None
    level: str = ""
    status_message: str = ""


class Tracer(Protocol):
    """追踪门面：`NullTracer` 与 `LangfuseTracer` 都满足它，测试可注入假实现。"""

    enabled: bool

    def run(self, *, name: str, metadata: dict[str, Any]) -> AbstractContextManager[None]: ...

    def hotspot(self, *, index: int, metadata: dict[str, Any],
                content: str = "") -> AbstractContextManager[None]: ...

    def generation(self, *, name: str, model: str,
                   metadata: dict[str, Any]) -> AbstractContextManager[Generation]: ...

    def finish_run(self, *, status: str, totals: dict[str, Any], run_id: str = "",
                   prompt_versions: dict[str, int] | None = None, error: str = "") -> None: ...

    def flush(self) -> None: ...


class NullTracer:
    """未启用追踪时的空实现：所有方法都是空操作，调用方不需要写分支。"""

    enabled = False

    def run(self, *, name: str, metadata: dict[str, Any]) -> AbstractContextManager[None]:
        return nullcontext(None)

    def hotspot(self, *, index: int, metadata: dict[str, Any],
                content: str = "") -> AbstractContextManager[None]:
        return nullcontext(None)

    def generation(self, *, name: str, model: str,
                   metadata: dict[str, Any]) -> AbstractContextManager[Generation]:
        return nullcontext(Generation(metadata=dict(metadata)))

    def finish_run(self, *, status: str, totals: dict[str, Any], run_id: str = "",
                   prompt_versions: dict[str, int] | None = None, error: str = "") -> None:
        return None

    def flush(self) -> None:
        return None


def build_client(cfg: AppConfig) -> Any:
    """造 Langfuse SDK 客户端（只在三件套齐全时调用）；延迟 import，缺依赖也不影响其它路径。"""
    from langfuse import Langfuse

    public, secret, host = cfg.langfuse_keys
    return Langfuse(public_key=public, secret_key=secret, host=host)


class LangfuseTracer:
    """把 span 交给 Langfuse SDK 的实现；客户端可注入（离线单测用假客户端）。"""

    enabled = True

    def __init__(self, cfg: AppConfig, *, client: Any | None = None) -> None:
        self.cfg = cfg
        self.capture = capture_content(cfg)
        self._client = client if client is not None else build_client(cfg)

    # ---------- 结构化上报 ----------
    @contextmanager
    def run(self, *, name: str, metadata: dict[str, Any]) -> Iterator[None]:
        """开一条 trace（v4 里根 span 就是 trace）。"""
        with self._client.start_as_current_observation(name=name, as_type="span",
                                                       metadata=dict(metadata)):
            yield None

    @contextmanager
    def hotspot(self, *, index: int, metadata: dict[str, Any],
                content: str = "") -> Iterator[None]:
        """一个热点的 span；只有 `capture_content` 打开时才带原文。"""
        with self._client.start_as_current_observation(
                name=f"hotspot-{index}", as_type="span",
                input=content if (self.capture and content) else None,
                metadata=dict(metadata)):
            yield None

    @contextmanager
    def generation(self, *, name: str, model: str,
                   metadata: dict[str, Any]) -> Iterator[Generation]:
        """一次模型调用的 generation：块内填 `Generation`，退出时写回 SDK。"""
        fields = Generation(metadata=dict(metadata))
        with self._client.start_as_current_observation(name=name, as_type="generation",
                                                       model=model, metadata=dict(metadata)):
            try:
                yield fields
            finally:
                self._apply_generation(fields)

    def _apply_generation(self, fields: Generation) -> None:
        payload: dict[str, Any] = {}
        if fields.usage_details:
            payload["usage_details"] = dict(fields.usage_details)
        if fields.cost_details:
            payload["cost_details"] = dict(fields.cost_details)
        if fields.metadata:
            payload["metadata"] = dict(fields.metadata)
        if self.capture:
            if fields.input is not None:
                payload["input"] = fields.input
            if fields.output is not None:
                payload["output"] = fields.output
        if fields.level:
            payload["level"] = fields.level
        if fields.status_message:
            payload["status_message"] = fields.status_message[:1000]
        if not payload:
            return
        try:
            self._client.update_current_generation(**payload)
        except Exception:   # noqa: BLE001 - 上报失败只记日志，绝不影响分析结果
            logger.warning("Langfuse generation 上报失败（不影响分析结果）", exc_info=True)

    def finish_run(self, *, status: str, totals: dict[str, Any], run_id: str = "",
                   prompt_versions: dict[str, int] | None = None, error: str = "") -> None:
        """收尾：把状态、统计与 prompt 版本写到 trace 上，并打一条带 trace 链接的日志。"""
        metadata: dict[str, Any] = {
            "status": status,
            "llm_calls": int(totals.get("llm_calls") or 0),
            "prompt_tokens": int(totals.get("prompt_tokens") or 0),
            "completion_tokens": int(totals.get("completion_tokens") or 0),
            "cost_cny": round(float(totals.get("cost_cny") or 0.0), 4),
            "latency_ms": int(totals.get("latency_ms") or 0),
        }
        if run_id:
            metadata["run_id"] = run_id
        if prompt_versions:
            metadata["prompt_versions"] = {str(key): int(value)
                                           for key, value in prompt_versions.items()}
        payload: dict[str, Any] = {
            "metadata": metadata,
            "level": LEVEL_ERROR if status != "succeeded" else LEVEL_DEFAULT,
        }
        if error:
            payload["status_message"] = error[:1000]
        try:
            self._client.update_current_span(**payload)
        except Exception:   # noqa: BLE001
            logger.warning("Langfuse trace 收尾失败（不影响分析结果）", exc_info=True)
        self._log_trace(run_id)

    def _log_trace(self, run_id: str) -> None:
        """把 trace id 与 UI 链接写进日志：按 run_id 回放时不用翻 Langfuse 界面找。"""
        try:
            trace_id = self._client.get_current_trace_id()
            url = self._client.get_trace_url(trace_id=trace_id) if trace_id else None
        except Exception:   # noqa: BLE001 - 取链接失败不影响上报
            return
        if trace_id:
            logger.info("调用追踪：run %s 的 trace %s%s", run_id or "-", trace_id,
                        f"（{url}）" if url else "")

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception:   # noqa: BLE001
            logger.warning("Langfuse flush 失败（不影响分析结果）", exc_info=True)


# 进程级单例（见模块 docstring 的"进程级懒加载单例"）：`_warned` 保证缺键时只警告一次
_tracer: Tracer | None = None
_lock = threading.Lock()
_warned = False


def get_tracer(cfg: AppConfig) -> Tracer:
    """进程级懒加载追踪器：三件套齐全造真客户端，否则 `NullTracer`（只警告一次）。"""
    global _tracer
    if _tracer is not None:
        return _tracer
    with _lock:
        if _tracer is None:
            enabled, reason = availability(cfg)
            if not enabled:
                _warn_disabled(reason)
                _tracer = NullTracer()
            else:
                try:
                    _tracer = LangfuseTracer(cfg)
                except Exception:   # noqa: BLE001 - 初始化失败也只是关闭追踪
                    logger.warning("Langfuse 客户端初始化失败，本次进程不上报追踪", exc_info=True)
                    _tracer = NullTracer()
    return _tracer


def warn_if_disabled(cfg: AppConfig) -> bool:
    """启动入口调用：缺键时打一条 warning（同一进程只打一次），返回追踪是否启用。"""
    enabled, reason = availability(cfg)
    if not enabled:
        _warn_disabled(reason)
    return enabled


def flush_tracer() -> None:
    """把缓冲里的 span 刷出去（worker 的 `worker_shutting_down` 调用；未启用时是空操作）。"""
    if _tracer is not None:
        _tracer.flush()


def reset_tracer() -> None:
    """清空进程级单例（测试用；下一次调用会重新判断可用性与警告状态）。"""
    global _tracer, _warned
    with _lock:
        _tracer = None
        _warned = False


def _warn_disabled(reason: str) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    logger.warning("调用追踪（Langfuse）未启用：%s", reason)


class TracingProvider:
    """包装一个 provider：每次 `complete()` 上报一个 generation（不侵入 `tools/llm.py`）。

    只依赖鸭子类型（`complete(LLMCall) -> LLMResult`），所以 `core/` 不必反向依赖 `tools/`；
    离线测试可直接传假 provider。上报失败绝不影响返回值（`LangfuseTracer` 内部吞掉异常）。
    """

    def __init__(self, inner: Any, tracer: Tracer, *, capture: bool = False,
                 price_in_per_m: float = 0.0, price_out_per_m: float = 0.0) -> None:
        self.inner = inner
        self.tracer = tracer
        self.capture = capture
        self.price_in_per_m = float(price_in_per_m)
        self.price_out_per_m = float(price_out_per_m)
        self.model = str(getattr(inner, "model", ""))

    @property
    def name(self) -> str:
        return str(getattr(self.inner, "name", "unknown"))

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}" if self.model else self.name

    async def complete(self, call: Any) -> Any:
        """转发给内层 provider，并把这几次调用的 token / 成本 / 结果状态上报出去。"""
        task = str(getattr(call, "task", "llm"))
        metadata = {"provider": self.name, "task": task}
        with self.tracer.generation(name=task, model=self.model, metadata=metadata) as fields:
            try:
                result = await self.inner.complete(call)
            except BaseException as exc:
                fields.level = LEVEL_ERROR
                fields.status_message = f"{type(exc).__name__}: {exc}"
                raise
            prompt_tokens = int(getattr(result, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(result, "completion_tokens", 0) or 0)
            in_cost = prompt_tokens / 1_000_000 * self.price_in_per_m
            out_cost = completion_tokens / 1_000_000 * self.price_out_per_m
            fields.usage_details = {"input": prompt_tokens, "output": completion_tokens,
                                    "total": prompt_tokens + completion_tokens}
            fields.cost_details = {"input": in_cost, "output": out_cost,
                                   "total": in_cost + out_cost}
            fields.metadata["attempts"] = int(getattr(result, "attempts", 1) or 1)
            error = str(getattr(result, "error", "") or "")
            if error:
                fields.level = LEVEL_ERROR
                fields.status_message = error
            if self.capture:
                fields.input = {"system": str(getattr(call, "system", "")),
                                "user": str(getattr(call, "user", ""))}
                fields.output = str(getattr(result, "text", "") or "")
            return result
