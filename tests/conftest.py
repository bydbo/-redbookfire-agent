"""跨 unit / integration 共用的测试夹具（S4.3 起）。

放根目录是因为 `otel_exporter` 两层都要用；integration 目录自己还有一套 testcontainers 夹具。
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from xhs_agent.core import tracing

_LANGFUSE_ENV = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
                 "LANGFUSE_BASE_URL", "LANGFUSE_CAPTURE_CONTENT")


@pytest.fixture(autouse=True)
def isolate_tracing(monkeypatch: pytest.MonkeyPatch):
    """测试里绝不使用本机的真实 Langfuse 凭据，也不让进程级单例跨用例串味。

    为什么需要：`config/.env` 里有真 key 时，任何走默认配置路径的用例（例如 lifespan 调
    `get_config()`）都会造出真客户端、真的往云上传 span；而进程级单例还会把上一条用例的
    tracer 带进下一条（S4.3 的链路用例就是被这个坑到过一次）。置空即"未配置"，
    `availability()` 会判定为关闭。
    """
    for name in _LANGFUSE_ENV:
        monkeypatch.setenv(name, "")
    tracing.reset_tracer()
    yield
    tracing.reset_tracer()


@pytest.fixture(scope="session")
def otel_exporter() -> InMemorySpanExporter:
    """真的 TracerProvider + 内存导出器（S4.3 的 OTel 用例共用一份）。

    `set_tracer_provider` 一个进程只能设一次，所以放 session 级：先看全局是不是已经是
    SDK provider（可能已被别的用例装过），不是才装，然后挂一个内存导出器。
    没有 provider 时 OTel 的所有埋点都是空操作，所以这个夹具是"让 span 可见"的开关。
    """
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter
