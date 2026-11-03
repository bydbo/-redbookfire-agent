"""`tools/websearch.py`（SearXNG 搜索适配器）的契约测试。

协议事实来自真实实例实测（探针见任务报告）：顶层只有
`query / results / answers / corrections / infoboxes / suggestions / unresponsive_engines` 七键，
**没有** `number_of_results`、也没有任何分页 / 总数字段；摘要字段是 `content`（不是 `snippet`）；
日期字段是 `publishedDate`，无日期时**键存在、值为 `null`。

所有用例只走 `httpx.MockTransport`：不联网、不依赖本机的 searxng 容器。
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
import pytest

from xhs_agent.config import AppConfig, EnvView, WebConfig
from xhs_agent.tools import websearch
from xhs_agent.tools.websearch import WebSearchError, search_web

SEARXNG = "http://searxng.test:8888"
WEB_ENV_KEY = "XHS_WEB_SEARCH_API_KEY"

# httpx.MockTransport 的处理函数签名
Handler = Callable[[httpx.Request], httpx.Response]


def make_cfg(**overrides) -> AppConfig:
    """默认 AppConfig + 指向假实例的 [web] 段；用例只覆盖自己关心的字段。"""
    cfg = AppConfig()
    cfg.web = WebConfig(**{"search_url": SEARXNG, **overrides})
    return cfg


def respond(body, *, status: int = 200,
            headers: dict[str, str] | None = None) -> Handler:
    """固定 JSON 响应的假 SearXNG 处理器。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers)

    return handler


def client_for(handler: Handler, calls: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    """把处理器包成带 MockTransport 的客户端，顺带记录请求。"""

    def wrapped(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


def payload(*results, unresponsive=None) -> dict:
    """真实实例的响应形状：只有这 7 个顶层键，键恒存在。"""
    return {
        "query": "羽毛球",
        "results": list(results),
        "answers": [],
        "corrections": [],
        "infoboxes": [],
        "suggestions": [],
        "unresponsive_engines": unresponsive or [],
    }


def raw_hit(title: str = "标题", url: str = "https://example.com/a",
            content: str = "摘要", published: str | None = None, **extra) -> dict:
    """单条 result 的实测字段形状（引擎数组是 `engines`，单值是 `engine`）。"""
    item = {
        "url": url,
        "title": title,
        "content": content,
        "publishedDate": published,
        "engine": "360search",
        "engines": ["360search"],
        "score": 1.0,
        "category": "general",
        "template": "default.html",
        "parsed_url": ["https", "example.com", "/a", "", "", ""],
        "positions": [{"1": 1}],
    }
    item.update(extra)
    return item


class TestParse:
    @pytest.mark.asyncio
    async def test_parses_searxng_json_into_hits(self):
        body = payload(
            raw_hit(title="羽毛球馆", url="https://example.com/a", content="第一段摘要",
                    published="2026-10-01T00:00:00+00:00"),
            raw_hit(title="球场预订", url="https://example.com/b", content="第二段摘要"),
            unresponsive=[["google", "timeout"]],
        )
        # 实测的七键：多一个少一个都说明自己造了协议
        assert set(body) == {"query", "results", "answers", "corrections", "infoboxes",
                            "suggestions", "unresponsive_engines"}
        client = client_for(respond(body))
        try:
            result = await search_web(client, make_cfg(), "羽毛球", count=5)
        finally:
            await client.aclose()
        assert [h.title for h in result.hits] == ["羽毛球馆", "球场预订"]
        assert [h.url for h in result.hits] == ["https://example.com/a",
                                               "https://example.com/b"]
        assert [h.snippet for h in result.hits] == ["第一段摘要", "第二段摘要"]
        assert result.hits[0].published_date == "2026-10-01T00:00:00+00:00"
        assert result.unresponsive == [["google", "timeout"]]

    @pytest.mark.asyncio
    async def test_content_becomes_snippet_and_is_truncated(self):
        long_text = "羽" * 300
        body = payload(raw_hit(content=long_text, snippet="不该被读到的字段"))
        client = client_for(respond(body))
        try:
            result = await search_web(client, make_cfg(snippet_chars=40), "羽毛球", count=3)
        finally:
            await client.aclose()
        assert result.hits[0].snippet == long_text[:40]
        assert len(result.hits[0].snippet) == 40

    @pytest.mark.asyncio
    async def test_published_date_null_is_kept_none(self):
        # 实测：没有日期时键存在、值是 null（不是缺键）
        body = payload(raw_hit(published=None))
        assert "publishedDate" in body["results"][0]
        client = client_for(respond(body))
        try:
            result = await search_web(client, make_cfg(), "羽毛球", count=3)
        finally:
            await client.aclose()
        assert len(result.hits) == 1
        assert result.hits[0].published_date is None

    @pytest.mark.asyncio
    async def test_drops_non_http_urls_and_dedupes(self):
        body = payload(
            raw_hit(title="A", url="https://example.com/a"),
            raw_hit(title="JS", url="javascript:alert(1)"),
            raw_hit(title="FTP", url="ftp://example.com/f"),
            raw_hit(title="空", url="   "),
            raw_hit(title="A 重复", url="https://example.com/a"),
            raw_hit(title="B", url="https://example.com/b"),
        )
        client = client_for(respond(body))
        try:
            result = await search_web(client, make_cfg(), "羽毛球", count=8)
        finally:
            await client.aclose()
        assert [h.title for h in result.hits] == ["A", "B"]
        assert [h.url for h in result.hits] == ["https://example.com/a",
                                               "https://example.com/b"]

    @pytest.mark.asyncio
    async def test_truncates_to_count(self):
        body = payload(*[raw_hit(title=f"第{i}条", url=f"https://example.com/{i}")
                         for i in range(5)])
        client = client_for(respond(body))
        try:
            first = await search_web(client, make_cfg(), "羽毛球", count=2)
            capped = await search_web(client, make_cfg(max_results=3), "羽毛球", count=99)
        finally:
            await client.aclose()
        assert [h.title for h in first.hits] == ["第0条", "第1条"]
        # count 再大也压到 max_results
        assert len(capped.hits) == 3


class TestErrors:
    @pytest.mark.asyncio
    async def test_403_maps_to_config_reason(self):
        # 实例没开 search.formats 的 json 时，实测是 403 + HTML（不是 JSON）
        client = client_for(
            lambda _request: httpx.Response(
                403, text="<html><body>Forbidden</body></html>",
                headers={"content-type": "text/html; charset=utf-8"}))
        try:
            with pytest.raises(WebSearchError) as excinfo:
                await search_web(client, make_cfg(), "羽毛球", count=3)
        finally:
            await client.aclose()
        assert excinfo.value.reason == "config"

    @pytest.mark.asyncio
    async def test_non_200_maps_to_http_reason(self):
        client = client_for(
            lambda _request: httpx.Response(500, json={"error": "boom"}))
        try:
            with pytest.raises(WebSearchError) as excinfo:
                await search_web(client, make_cfg(), "羽毛球", count=3)
        finally:
            await client.aclose()
        assert excinfo.value.reason == "http"

    @pytest.mark.asyncio
    async def test_200_with_html_body_maps_to_schema_reason(self):
        # 非法 format 名会 200 + HTML 静默降级。这里故意让 body 是**可解析的 JSON**：
        # 只有 content-type 那道闸能拦住它——否则会被当成正常结果静默返回 0 命中
        client = client_for(
            lambda _request: httpx.Response(
                200, content=b'{"results": []}',
                headers={"content-type": "text/html; charset=utf-8"}))
        try:
            with pytest.raises(WebSearchError) as excinfo:
                await search_web(client, make_cfg(), "羽毛球", count=3)
        finally:
            await client.aclose()
        assert excinfo.value.reason == "schema"

    @pytest.mark.asyncio
    async def test_blank_search_url_maps_to_config_reason(self):
        # WebConfig.search_url 默认就是空串：默认配置下最可能走到的一条路径，且不该发请求
        calls: list[httpx.Request] = []
        client = client_for(respond(payload()), calls)
        try:
            with pytest.raises(WebSearchError) as excinfo:
                await search_web(client, make_cfg(search_url=""), "羽毛球", count=3)
        finally:
            await client.aclose()
        assert excinfo.value.reason == "config"
        assert calls == []

    @pytest.mark.asyncio
    async def test_bad_json_maps_to_schema_reason(self):
        client = client_for(
            lambda _request: httpx.Response(
                200, content=b'{"results": [',
                headers={"content-type": "application/json"}))
        try:
            with pytest.raises(WebSearchError) as excinfo:
                await search_web(client, make_cfg(), "羽毛球", count=3)
        finally:
            await client.aclose()
        assert excinfo.value.reason == "schema"

    @pytest.mark.asyncio
    async def test_timeout_maps_to_timeout_reason(self):
        # 预热一次：本机首次建 AsyncClient 约 1.3s（certifi 加载），不预热会量到这份开销
        warm = client_for(respond(payload()))
        try:
            await search_web(warm, make_cfg(), "预热", count=1)
        finally:
            await warm.aclose()

        seen: dict[str, dict[str, float]] = {}

        def slow(request: httpx.Request) -> httpx.Response:
            seen["timeout"] = request.extensions["timeout"]
            raise httpx.ReadTimeout("timed out", request=request)

        client = client_for(slow)
        try:
            started = time.perf_counter()
            with pytest.raises(WebSearchError) as excinfo:
                await search_web(client, make_cfg(search_timeout_s=3), "羽毛球", count=3)
            elapsed = time.perf_counter() - started
        finally:
            await client.aclose()
        assert excinfo.value.reason == "timeout"
        # 复用注入的 client 时也必须显式传 timeout（共享 client 是 120s）
        assert seen["timeout"]["read"] == 3.0
        assert elapsed < 2.0        # 超时立即失败，不重试


class TestRequest:
    @pytest.mark.asyncio
    async def test_engines_param_sent_only_when_configured(self):
        calls: list[httpx.Request] = []
        client = client_for(respond(payload()), calls)
        try:
            await search_web(client, make_cfg(), "羽毛球", count=3)
            await search_web(client, make_cfg(engines="360search,yandex"), "羽毛球", count=3)
        finally:
            await client.aclose()
        assert "engines" not in calls[0].url.params
        assert calls[1].url.params["engines"] == "360search,yandex"

    @pytest.mark.asyncio
    async def test_fixed_params_are_sent(self):
        calls: list[httpx.Request] = []
        client = client_for(respond(payload()), calls)
        try:
            await search_web(client, make_cfg(), "羽毛球", count=3)
        finally:
            await client.aclose()
        params = calls[0].url.params
        assert str(calls[0].url).startswith(f"{SEARXNG}/search?")
        assert params["q"] == "羽毛球"
        assert params["format"] == "json"
        assert params["language"] == "zh-CN"
        assert params["safesearch"] == "1"
        assert params["pageno"] == "1"

    @pytest.mark.asyncio
    async def test_api_key_becomes_bearer_header(self):
        calls: list[httpx.Request] = []
        client = client_for(respond(payload()), calls)
        cfg = make_cfg()
        cfg.web._env = EnvView({}, {WEB_ENV_KEY: "sk-web-secret"})
        try:
            await search_web(client, cfg, "羽毛球", count=3)
            await search_web(client, make_cfg(), "羽毛球", count=3)   # 没密钥
        finally:
            await client.aclose()
        assert calls[0].headers["authorization"] == "Bearer sk-web-secret"
        assert "authorization" not in calls[1].headers


class TestEmptyAndClient:
    @pytest.mark.asyncio
    async def test_empty_results_returns_empty_hits_not_error(self):
        body = payload(unresponsive=[["google", "timeout"]])
        client = client_for(respond(body))
        try:
            result = await search_web(client, make_cfg(), "羽毛球", count=3)
        finally:
            await client.aclose()
        assert result.hits == []
        assert result.unresponsive == [["google", "timeout"]]

    @pytest.mark.asyncio
    async def test_client_none_builds_temporary_client(self, monkeypatch):
        built: dict[str, object] = {}

        def fake_build(timeout_s: float) -> httpx.AsyncClient:
            built["timeout_s"] = timeout_s
            made = httpx.AsyncClient(transport=httpx.MockTransport(respond(payload(raw_hit()))))
            built["client"] = made
            return made

        monkeypatch.setattr(websearch, "build_http_client", fake_build)
        result = await search_web(None, make_cfg(search_timeout_s=7), "羽毛球", count=3)
        assert built["timeout_s"] == 7
        assert len(result.hits) == 1
        assert built["client"].is_closed          # 自建的临时客户端必须关掉
