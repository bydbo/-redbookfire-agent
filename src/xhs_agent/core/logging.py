"""日志：按配置的日志级别初始化标准库 logging，并让每条记录带上 request_id。

用途：`configure_logging(level)` 幂等配置；`RequestIdFilter` 把当前请求 id 注入每条日志记录
      （`record.request_id`），便于按请求把一次调用的日志串起来。
输入：日志级别字符串（`DEBUG` / `INFO` / `WARNING` / `ERROR`，非法值退回 `INFO`）。
输出：无（配置 logging 的副作用）；`current_request_id()` 读取当前请求 id（不在请求里时为空串）。

口径：不引入 structlog（E4 的事）；不写日志文件、不加自己的 handler——日志走 stdout 由容器收集。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

# 与《配置契约》§3.9 的 log_level 取值一致
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")
FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def current_request_id() -> str:
    """当前请求的 id；不在请求上下文里（如离线脚本）返回空串。"""
    return request_id_var.get()


class RequestIdFilter(logging.Filter):
    """给每条日志记录补上 `request_id`（缺省空串，避免格式化时 KeyError）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        return True


def configure_logging(level: str = "INFO") -> None:
    """幂等配置根 logger：设置级别、格式与 request_id 过滤器。

    只配置现有 handler（uvicorn / pytest 已经装好的那些），不新增 handler，
    这样与框架自己的日志装配不打架；重复调用只更新级别、不重复加过滤器。
    """
    selected = str(level or "").strip().upper()
    if selected not in LOG_LEVELS:
        selected = "INFO"
    numeric = getattr(logging, selected)

    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=numeric, format=FORMAT)
    root.setLevel(numeric)
    for handler in root.handlers:
        if not any(isinstance(item, RequestIdFilter) for item in handler.filters):
            handler.addFilter(RequestIdFilter())
