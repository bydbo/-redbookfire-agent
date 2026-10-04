"""OpenTelemetry 埋点与跨进程传播的单测（S4.3）：真 TracerProvider + 内存导出器，不联网。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry import trace

from xhs_agent.api.main import create_app
from xhs_agent.core.tracing import (
    DB_TRACER_NAME,
    current_trace_headers,
    db_span,
    install_fastapi_instrumentation,
    should_export_span,
    traced_db,
    use_trace_carrier,
)


@pytest.fixture
def exporter(otel_exporter):
    otel_exporter.clear()
    return otel_exporter


def fake_span(name: str, attributes: dict[str, Any] | None = None) -> SimpleNamespace:
    """伪 ReadableSpan：Langfuse 的默认过滤器只读 instrumentation_scope 与 attributes。"""
    return SimpleNamespace(instrumentation_scope=SimpleNamespace(name=name),
                           attributes=attributes or {})


class TestExportFilter:
    def test_allows_our_scopes_and_instrumentation(self):
        assert should_export_span(fake_span(DB_TRACER_NAME)) is True
        assert should_export_span(fake_span("opentelemetry.instrumentation.fastapi")) is True
        assert should_export_span(fake_span("opentelemetry.instrumentation.asgi")) is True

    def test_rejects_unrelated_scopes(self):
        assert should_export_span(fake_span("some.random.library")) is False

    def test_keeps_langfuse_default_rule(self):
        """gen_ai.* 之类的默认规则不能被我们覆盖掉。"""
        allowed = should_export_span(fake_span("some.random.library", {"gen_ai.system": "openai"}))
        assert allowed is True


class TestDbSpan:
    def test_attributes_carry_operation_and_tables_but_no_statement(self, exporter):
        with db_span("db.submit_analysis", operation="insert",
                     tables=("hotspots", "runs", "run_hotspots")):
            pass

        (span,) = exporter.get_finished_spans()
        assert span.name == "db.submit_analysis"
        assert span.instrumentation_scope.name == DB_TRACER_NAME
        attributes = dict(span.attributes or {})
        assert attributes["db.system"] == "postgresql"
        assert attributes["db.operation"] == "insert"
        assert json.loads(attributes["langfuse.observation.metadata.db.tables"]) == [
            "hotspots", "runs", "run_hotspots"]
        assert not any("statement" in key.lower() for key in attributes)   # 不上报 SQL 语句

    @pytest.mark.asyncio
    async def test_traced_db_decorator_wraps_async_function(self, exporter):
        @traced_db("db.load_job", operation="select", tables=("runs",))
        async def fake_load(value: int) -> int:
            return value + 1

        assert await fake_load(41) == 42
        (span,) = exporter.get_finished_spans()
        assert span.name == "db.load_job"
        assert dict(span.attributes or {})["db.operation"] == "select"


class TestPropagation:
    def test_headers_are_empty_without_an_active_span(self):
        assert current_trace_headers() == {}

    def test_headers_carry_traceparent_inside_a_span(self):
        tracer = trace.get_tracer("xhs_agent.test")
        with tracer.start_as_current_span("op") as span:
            headers = current_trace_headers()
        assert set(headers) == {"traceparent"}
        assert headers["traceparent"].startswith("00-")
        assert f"{span.get_span_context().trace_id:032x}" in headers["traceparent"]

    def test_worker_joins_the_upstream_trace(self, exporter):
        with trace.get_tracer("upstream").start_as_current_span("upstream") as upstream:
            headers = current_trace_headers()
            upstream_span_id = upstream.get_span_context().span_id

        with (use_trace_carrier(headers),
              db_span("db.run_recorder.finish", operation="update", tables=("runs",))):
            pass

        finished = {span.name: span for span in exporter.get_finished_spans()}
        child = finished["db.run_recorder.finish"]
        assert child.context.trace_id == upstream.context.trace_id      # 同一个 trace
        assert child.parent is not None and child.parent.span_id == upstream_span_id

    def test_empty_carrier_keeps_working_without_a_parent(self, exporter):
        with (use_trace_carrier(None),
              db_span("db.load_run", operation="select", tables=("runs",))):
            pass
        (span,) = exporter.get_finished_spans()
        assert span.parent is None                                       # 自己开一条新 trace


class TestFastApiInstrumentation:
    def test_installed_once_per_app(self):
        app = create_app(check_startup=False)
        assert app.state.otel_instrumented is True        # create_app 里已装
        assert install_fastapi_instrumentation(app) is False

    def test_bare_app_gets_instrumented(self):
        from fastapi import FastAPI

        app = FastAPI()
        assert install_fastapi_instrumentation(app) is True
        assert install_fastapi_instrumentation(app) is False
