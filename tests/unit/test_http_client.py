"""httpx 客户端基座单测（S3.5）：连接池参数、超时、`async with` 关闭语义与退避算法。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from xhs_agent.tools import http as http_tool


class TestBuildHttpClient:
    def test_passes_pool_limits_and_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """把构造参数记下来：连接池上限与超时是这一层唯一的职责。"""
        captured: dict[str, Any] = {}

        class FakeClient:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        monkeypatch.setattr(http_tool.httpx, "AsyncClient", FakeClient)
        client = http_tool.build_http_client(7)

        assert isinstance(client, FakeClient)
        assert captured["timeout"] == httpx.Timeout(7)
        assert captured["limits"] == httpx.Limits(
            max_connections=http_tool.MAX_CONNECTIONS,
            max_keepalive_connections=http_tool.MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=http_tool.KEEPALIVE_EXPIRY_S)
        assert captured["follow_redirects"] is False

    def test_default_timeout_is_applied(self, monkeypatch: pytest.MonkeyPatch):
        captured: dict[str, Any] = {}
        monkeypatch.setattr(http_tool.httpx, "AsyncClient",
                            lambda **kwargs: captured.update(kwargs))
        http_tool.build_http_client()
        assert captured["timeout"] == httpx.Timeout(http_tool.DEFAULT_TIMEOUT_S)

    @pytest.mark.asyncio
    async def test_async_with_closes_the_client(self):
        async with http_tool.build_http_client(5) as client:
            assert not client.is_closed
        assert client.is_closed

    def test_pool_limits_are_the_documented_values(self):
        assert (http_tool.MAX_CONNECTIONS, http_tool.MAX_KEEPALIVE_CONNECTIONS,
                http_tool.KEEPALIVE_EXPIRY_S) == (10, 5, 30.0)


class TestBackoffSeconds:
    def test_grows_by_one_and_a_half(self):
        assert http_tool.backoff_seconds(1) == pytest.approx(1.5)
        assert http_tool.backoff_seconds(2) == pytest.approx(2.25)
        assert http_tool.backoff_seconds(3) == pytest.approx(3.375)
        assert http_tool.backoff_seconds(4) == pytest.approx(5.0625)
        assert http_tool.backoff_seconds(5) == pytest.approx(7.59375)

    def test_caps_at_eight_seconds(self):
        assert http_tool.backoff_seconds(6) == pytest.approx(8.0)
        assert http_tool.backoff_seconds(50) == pytest.approx(8.0)

    def test_non_positive_attempt_is_floored_to_one(self):
        assert http_tool.backoff_seconds(0) == pytest.approx(1.5)
        assert http_tool.backoff_seconds(-3) == pytest.approx(1.5)
