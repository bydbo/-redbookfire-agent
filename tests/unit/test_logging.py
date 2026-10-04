"""日志层单测（S4.1）：structlog 管线、两个 id 恒在、run_id 上下文绑定。

离线、不读真实密钥、不连库：只在一个自建 handler 上验证输出形状。
"""

from __future__ import annotations

import io
import json
import logging
import re

import pytest
from structlog.stdlib import ProcessorFormatter

from xhs_agent.core.logging import (
    LOG_FORMATS,
    LOG_LEVELS,
    TraceIdFilter,
    bind_run_id,
    configure_logging,
    current_request_id,
    current_run_id,
    request_id_var,
    reset_run_id,
)

LOGGER_NAME = "xhs_agent.test.logging"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def restore_root_logging():
    """每个用例后把根 logger 的级别 / 格式 / 过滤器还原，避免污染其它用例。"""
    root = logging.getLogger()
    level = root.level
    before = list(root.handlers)
    snapshots = [(handler, handler.formatter, list(handler.filters)) for handler in before]
    yield
    root.setLevel(level)
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
    for handler, formatter, filters in snapshots:
        handler.setFormatter(formatter)
        handler.filters = filters


@pytest.fixture
def capture():
    """往根 logger 挂一个捕获 handler，返回它写入的缓冲区。"""
    buffer = io.StringIO()
    root = logging.getLogger()
    root.addHandler(logging.StreamHandler(buffer))
    return buffer


def rows(buffer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buffer.getvalue().strip().splitlines() if line.strip()]


class TestJsonFormat:
    def test_each_line_is_a_queryable_json_object(self, capture) -> None:
        configure_logging("INFO", "json")
        logging.getLogger(LOGGER_NAME).info("索引新鲜度：%s", "3 条素材")

        (row,) = rows(capture)
        assert row["event"] == "索引新鲜度：3 条素材"     # %s 语法照旧生效
        assert row["level"] == "info"
        assert row["logger"] == LOGGER_NAME
        assert isinstance(row["timestamp"], str) and row["timestamp"].endswith("Z")

    def test_both_ids_are_always_present(self, capture) -> None:
        configure_logging("INFO", "json")
        logging.getLogger(LOGGER_NAME).info("没有上下文")

        (row,) = rows(capture)
        assert row["request_id"] == "" and row["run_id"] == ""

    def test_ids_come_from_context_vars(self, capture) -> None:
        configure_logging("INFO", "json")
        request_id_var.set("req-1")
        token = bind_run_id("run-1")
        try:
            logging.getLogger(LOGGER_NAME).info("带上下文")
        finally:
            reset_run_id(token)
            request_id_var.set("")

        (row,) = rows(capture)
        assert row["request_id"] == "req-1"
        assert row["run_id"] == "run-1"

    def test_exception_text_goes_to_the_log(self, capture) -> None:
        configure_logging("INFO", "json")
        try:
            raise ValueError("坏掉了")
        except ValueError:
            logging.getLogger(LOGGER_NAME).exception("异常路径")

        (row,) = rows(capture)
        assert row["level"] == "error"
        assert "ValueError: 坏掉了" in str(row["exception"])


class TestConsoleFormat:
    def test_console_stays_human_readable(self, capture) -> None:
        configure_logging("INFO", "console")
        logging.getLogger(LOGGER_NAME).info("控制台模式")

        text = capture.getvalue()
        assert "控制台模式" in text
        assert not text.lstrip().startswith("{")     # 不是 JSON 行
        plain = ANSI.sub("", text)                   # 终端着色时键与值之间会夹 ANSI 序列
        assert "request_id=" in plain and "run_id=" in plain


class TestRunIdContext:
    def test_bind_and_reset_are_nested(self) -> None:
        assert current_run_id() == ""
        outer = bind_run_id("run-outer")
        inner = bind_run_id("run-inner")
        assert current_run_id() == "run-inner"
        reset_run_id(inner)
        assert current_run_id() == "run-outer"
        reset_run_id(outer)
        assert current_run_id() == ""

    def test_request_id_is_untouched_by_run_id(self) -> None:
        request_id_var.set("req-x")
        token = bind_run_id("run-x")
        try:
            assert current_request_id() == "req-x"
            assert current_run_id() == "run-x"
        finally:
            reset_run_id(token)
            request_id_var.set("")


class TestConfigureLogging:
    def test_level_and_format_take_effect(self, capture) -> None:
        configure_logging("WARNING", "json")
        root = logging.getLogger()
        assert root.level == logging.WARNING
        assert root.handlers
        assert all(isinstance(item.formatter, ProcessorFormatter) for item in root.handlers)

        configure_logging("INFO", "console")
        assert root.level == logging.INFO
        logging.getLogger(LOGGER_NAME).info("切回控制台")
        assert not capture.getvalue().lstrip().startswith("{")

    def test_repeated_calls_do_not_duplicate_filters(self) -> None:
        configure_logging("INFO", "json")
        configure_logging("INFO", "json")
        for handler in logging.getLogger().handlers:
            installed = [item for item in handler.filters if isinstance(item, TraceIdFilter)]
            assert len(installed) == 1

    def test_invalid_values_fall_back(self, capture) -> None:
        configure_logging("TRACE", "xml")
        root = logging.getLogger()
        assert root.level == logging.INFO
        logging.getLogger(LOGGER_NAME).info("兜底")
        assert not capture.getvalue().lstrip().startswith("{")

    def test_explicit_extra_is_not_overwritten(self, capture) -> None:
        """调用方显式给的 run_id 不被上下文里的值覆盖（访问日志走这条路）。"""
        configure_logging("INFO", "json")
        token = bind_run_id("run-from-context")
        try:
            logging.getLogger(LOGGER_NAME).info("显式指定", extra={"run_id": "run-explicit"})
        finally:
            reset_run_id(token)

        (row,) = rows(capture)
        assert row["run_id"] == "run-explicit"

    def test_contract_enums_match_docs(self) -> None:
        assert LOG_LEVELS == ("DEBUG", "INFO", "WARNING", "ERROR")
        assert LOG_FORMATS == ("console", "json")
