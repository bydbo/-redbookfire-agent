"""日志：标准库 logging 的调用 API + structlog 的格式化管线（S4.1）。

用途：`configure_logging(level, log_format)` 幂等配置根 logger 与框架 logger（uvicorn / celery）；
      `TraceIdFilter` 给每条记录补上 `request_id` 与 `run_id`（缺省空串），一次请求 / 一次运行的
      日志因此可以按 id 串起来。
输入：日志级别（`DEBUG` / `INFO` / `WARNING` / `ERROR`，非法值退回 `INFO`）与输出格式
      （`console` / `json`，非法值退回 `console`）。
输出：无（配置 logging 的副作用）；`current_request_id()` / `current_run_id()` 读当前上下文。
      `bind_run_id()` / `reset_run_id()` 用于把 run_id 绑进（与解绑）当前上下文。

口径（S4.1 定下，改动需同步 `docs/开发规范.md`）：

- **调用点仍用标准库 logging**（`logging.getLogger(...).info("…%s", x)` 全部保留），structlog 只做
  格式化管线（`ProcessorFormatter` + `JSONRenderer` / `ConsoleRenderer`），**不调**
  `structlog.get_logger()`——这样与 uvicorn / pytest 各自的日志装配不打架。
- **`request_id` 与 `run_id` 两个键恒在**：不在上下文里时为空串，JSON 输出可直接用 `jq` 过滤。
- `log_format = "json"` 时一行一个 JSON 对象（容器 / CI 用）；默认 `console` 保留本机可读性。
- 异常栈只进日志（JSON 的 `exception` 字段），不进 HTTP 响应。
- 不写日志文件、不加自己的 handler——日志走 stdout / stderr 由容器收集。
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from contextvars import ContextVar, Token
from typing import Any

import structlog
from structlog.stdlib import ProcessorFormatter
from structlog.typing import Processor

# 与《配置契约》§3.10 的 log_level / log_format 取值一致
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMATS: tuple[str, ...] = ("console", "json")

# 自带 handler 且不向根传播的框架 logger：格式要单独装，否则容器里会混进非 JSON 行
FRAMEWORK_LOGGERS: tuple[str, ...] = ("uvicorn", "uvicorn.error", "uvicorn.access",
                                      "celery", "celery.app.trace")

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
run_id_var: ContextVar[str] = ContextVar("run_id", default="")


def current_request_id() -> str:
    """当前请求的 id；不在请求上下文里（如离线脚本）返回空串。"""
    return request_id_var.get()


def current_run_id() -> str:
    """当前运行的 id；不在运行上下文里（如启动阶段）返回空串。"""
    return run_id_var.get()


def bind_run_id(value: str) -> Token[str]:
    """把 run_id 绑进当前上下文；返回的 token 交给 `reset_run_id` 解绑。"""
    return run_id_var.set(str(value or ""))


def reset_run_id(token: Token[str]) -> None:
    """解绑 `bind_run_id` 绑上的 run_id（恢复绑定前的值）。"""
    run_id_var.reset(token)


class TraceIdFilter(logging.Filter):
    """给每条日志记录补上 `request_id` 与 `run_id`（缺省空串，避免格式化时 KeyError）。

    调用方用 `extra={...}` 显式给的值不被覆盖——那是调用方明确指定的。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", ""):
            record.request_id = current_request_id()
        if not getattr(record, "run_id", ""):
            record.run_id = current_run_id()
        return True


# S3.2 落地时的名字；语义不变，保留别名避免调用方与测试失效
RequestIdFilter = TraceIdFilter


def _ensure_trace_ids(logger: Any, method_name: str,
                      event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """structlog 处理器：保证 `request_id` / `run_id` 两个键恒在（缺省空串）。"""
    if not event_dict.get("request_id"):
        event_dict["request_id"] = ""
    if not event_dict.get("run_id"):
        event_dict["run_id"] = ""
    return event_dict


def _pre_chain() -> list[Processor]:
    """标准库记录进入 structlog 管线前要走的处理器（每次重建：TimeStamper 无状态但便宜）。"""
    return [
        # ExtraAdder 把 filter 写在记录上的两个 id 带进 event_dict（否则标准库记录只映射
        # `event` 一个字段）；只放行这两个键，避免把 LogRecord 的杂项字段灌进 JSON。
        structlog.stdlib.ExtraAdder(allow=("request_id", "run_id")),
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        _ensure_trace_ids,
    ]


def build_formatter(log_format: str) -> ProcessorFormatter:
    """按格式造渲染器：`json` → 一行一个 JSON 对象；其余 → 彩色控制台（非 TTY 自动降级）。"""
    selected = str(log_format or "").strip().lower()
    renderer: Processor = (structlog.processors.JSONRenderer(ensure_ascii=False, sort_keys=True)
                           if selected == "json"
                           else structlog.dev.ConsoleRenderer())
    return ProcessorFormatter(processor=renderer, foreign_pre_chain=_pre_chain())


def _install(handler: logging.Handler, formatter: ProcessorFormatter) -> None:
    """给一个 handler 装上结构化格式与 id 过滤器（重复调用不重复加过滤器）。"""
    handler.setFormatter(formatter)
    if not any(isinstance(item, TraceIdFilter) for item in handler.filters):
        handler.addFilter(TraceIdFilter())


def configure_logging(level: str = "INFO", log_format: str = "console") -> None:
    """幂等配置日志：级别 + 输出格式 + id 过滤器。

    只配置**已存在**的 handler（uvicorn / celery / pytest 装好的那些），不够就补一个 StreamHandler；
    重复调用只更新级别与格式，不重复加 handler / filter。
    """
    selected = str(level or "").strip().upper()
    if selected not in LOG_LEVELS:
        selected = "INFO"
    numeric = getattr(logging, selected)
    formatter = build_formatter(log_format)

    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    root.setLevel(numeric)
    for handler in root.handlers:
        _install(handler, formatter)

    # 框架自带的 handler 不向根传播，格式要单独装（否则 JSON 模式里会混进纯文本行）
    for name in FRAMEWORK_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            _install(handler, formatter)
