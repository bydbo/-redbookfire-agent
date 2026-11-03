"""网页工具的清单与开关（PR2 · Task 5）：`tools_for` / `web_enabled` / `effective_max_rounds`。

A1 的机器可验证据是 `test_tools_for_disabled_equals_tools`：关闭态下发的清单必须与
`list(TOOLS)` **逐项相等**——多一条就说明默认能力被悄悄改了。本任务只下发清单、不注册
handler（handler 在 Task 6），所以两条网页 schema 放在 `TOOLS` 之外，`tool_names()` 的
无参行为保持全量 = `TOOLS`。

两条 schema 的 description **就是 prompt**（计划逐字钉死）：模型靠它决定该不该调、
不该调到哪。所以这里既钉"有没有"，也钉那几句必含文案。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest

from xhs_agent.config import PROJECT_ROOT, AppConfig, EnvView, WebConfig
from xhs_agent.services import chat_tools
from xhs_agent.services.chat_tools import (
    HANDLERS,
    TOOL_FETCH_URL,
    TOOL_SCHEMA_FETCH_URL,
    TOOL_SCHEMA_SEARCH_HOTSPOTS,
    TOOL_SEARCH_HOTSPOTS,
    TOOLS,
    WEB_MAX_TOOL_ROUNDS,
    ToolCache,
    ToolContext,
    effective_max_rounds,
    tool_names,
    tools_for,
    web_enabled,
)
from xhs_agent.tools.webreader import FetchedPage, WebFetchError
from xhs_agent.tools.websearch import SearchHit, SearchResult, WebSearchError


def make_cfg(**web: Any) -> AppConfig:
    """默认 AppConfig + 指定的 `[web]` 覆盖；用例只写自己关心的开关。"""
    cfg = AppConfig()
    cfg.web = WebConfig(**web)
    return cfg


def names_of(tools: list[dict[str, Any]]) -> list[str]:
    return [str(item["function"]["name"]) for item in tools]


class TestToolsFor:
    def test_tools_for_disabled_equals_tools(self) -> None:
        """A1：关闭态逐项相等（顺序也算）——追加工具时不许重排老工具。"""
        assert tools_for(make_cfg(enabled=False)) == list(TOOLS)

    def test_tools_for_none_equals_tools(self) -> None:
        """`cfg=None`（离线评测 / 还没接线的调用方）与关闭态同义。"""
        assert tools_for(None) == list(TOOLS)
        assert tools_for() == list(TOOLS)

    def test_tools_for_enabled_adds_search_only_when_fetch_disabled(self) -> None:
        tools = tools_for(make_cfg(enabled=True, fetch_enabled=False))
        assert tools[: len(TOOLS)] == list(TOOLS)
        assert names_of(tools) == [*names_of(TOOLS), TOOL_SEARCH_HOTSPOTS]

    def test_tools_for_enabled_adds_both(self) -> None:
        tools = tools_for(make_cfg(enabled=True))
        assert tools[: len(TOOLS)] == list(TOOLS)
        assert names_of(tools) == [*names_of(TOOLS), TOOL_SEARCH_HOTSPOTS, TOOL_FETCH_URL]

    def test_web_tools_are_not_in_default_tools(self) -> None:
        """两条 schema 不进 `TOOLS`：否则关闭态清单、分发表、prompt 工具清单同时被改坏。"""
        assert TOOL_SEARCH_HOTSPOTS not in names_of(TOOLS)
        assert TOOL_FETCH_URL not in names_of(TOOLS)

    def test_tools_for_returns_a_fresh_list(self) -> None:
        """每次现算：往返回值里多加一条，不许活到下一次调用。"""
        first = tools_for(make_cfg(enabled=True))
        first.append({"type": "function", "function": {"name": "fake"}})
        assert names_of(tools_for(make_cfg(enabled=True))) == [
            *names_of(TOOLS),
            TOOL_SEARCH_HOTSPOTS,
            TOOL_FETCH_URL,
        ]

    def test_tools_for_deep_copies_schemas(self) -> None:
        """就地改返回值里的**嵌套**字段，不许回流到模块常量（下一轮必须还是原值）。

        `TOOLS` 的元素与 `TOOL_SCHEMA_*` 都是模块级 dict：浅拷贝等于把全局清单借给调用方。
        """
        original = TOOLS[0]["function"]["description"]
        tools = tools_for(make_cfg(enabled=True))
        tools[0]["function"]["description"] = "被调用方改坏了"
        tools[-1]["function"]["parameters"]["properties"]["url"]["description"] = "被调用方改坏了"
        off = tools_for(None)
        off[0]["function"]["description"] = "被调用方改坏了"
        assert TOOLS[0]["function"]["description"] == original
        assert tools_for(make_cfg(enabled=True)) == [
            *TOOLS,
            TOOL_SCHEMA_SEARCH_HOTSPOTS,
            TOOL_SCHEMA_FETCH_URL,
        ]
        assert tools_for(None) == list(TOOLS)


class TestToolNames:
    def test_tool_names_none_returns_full_set(self) -> None:
        """旧行为不变：无参 = `TOOLS` 里的全部名字（分发表、prompt 清单都靠它）。"""
        assert tool_names() == set(names_of(TOOLS))
        assert tool_names(None) == tool_names()

    def test_tool_names_with_web_enabled_includes_web_tools(self) -> None:
        """Task 7 的"未知工具"判定读这个：关闭态认不出网页工具名，开启态认得。"""
        assert tool_names(make_cfg(enabled=True)) == {
            *names_of(TOOLS),
            TOOL_SEARCH_HOTSPOTS,
            TOOL_FETCH_URL,
        }
        assert tool_names(make_cfg(enabled=True, fetch_enabled=False)) == {
            *names_of(TOOLS),
            TOOL_SEARCH_HOTSPOTS,
        }


class TestWebEnabled:
    def test_web_enabled_ignores_api_key(self) -> None:
        """私有 SearXNG 无鉴权：密钥不是启用前提，开关才是唯一判据。"""
        cfg = make_cfg(enabled=True)
        assert cfg.web.api_key == ""  # 默认没有环境视图 → 密钥为空
        assert web_enabled(cfg) is True

        cfg.web._env = EnvView({}, {"XHS_WEB_SEARCH_API_KEY": "sk-x"})  # 注入一份带密钥的视图
        assert cfg.web.api_key == "sk-x"

        off = make_cfg(enabled=False)
        off.web._env = EnvView({}, {"XHS_WEB_SEARCH_API_KEY": "sk-x"})
        assert off.web.api_key == "sk-x"
        assert web_enabled(off) is False


class TestEffectiveMaxRounds:
    def test_effective_max_rounds_default_three(self) -> None:
        """关闭态一字不动：`[chat].max_tool_rounds` 是多少就多少（默认 3）。"""
        cfg = make_cfg()
        assert cfg.chat.max_tool_rounds == 3
        assert effective_max_rounds(cfg) == 3

    def test_effective_max_rounds_web_on_is_at_least_eight(self) -> None:
        """网页工具是多跳的（搜完要读、读完要接着读），3 轮不够用。"""
        assert WEB_MAX_TOOL_ROUNDS == 8
        assert effective_max_rounds(make_cfg(enabled=True)) >= WEB_MAX_TOOL_ROUNDS

    def test_effective_max_rounds_keeps_larger_chat_value(self) -> None:
        """语义是 `max(...)` 而不是写死 8：用户把 `[chat]` 调大时不许被压回去。"""
        cfg = make_cfg(enabled=True)
        cfg.chat.max_tool_rounds = 10
        assert effective_max_rounds(cfg) == 10


class TestWebToolSchemas:
    def test_search_hotspots_schema(self) -> None:
        function = TOOL_SCHEMA_SEARCH_HOTSPOTS["function"]
        assert TOOL_SCHEMA_SEARCH_HOTSPOTS["type"] == "function"
        assert function["name"] == TOOL_SEARCH_HOTSPOTS
        assert len(function["description"]) > 20
        assert "只用于发现**可以蹭**的热点；不用于天气、新闻、百科、股价等通用信息查询" in (
            function["description"]
        )
        parameters = function["parameters"]
        assert parameters["type"] == "object"
        assert parameters["required"] == ["query"]
        properties = parameters["properties"]
        assert properties["query"]["type"] == "string"
        assert "用户想找什么方向的热点" in properties["query"]["description"]
        assert properties["count"]["type"] == "integer"
        assert (properties["count"]["minimum"], properties["count"]["maximum"]) == (1, 10)
        assert "默认 5" in properties["count"]["description"]

    def test_fetch_url_schema(self) -> None:
        function = TOOL_SCHEMA_FETCH_URL["function"]
        assert TOOL_SCHEMA_FETCH_URL["type"] == "function"
        assert function["name"] == TOOL_FETCH_URL
        description = function["description"]
        assert "只读取搜索结果里给出的链接，或用户直接贴出的链接；不要自己拼 URL" in description
        assert "内容被截断时用 `start_index` 接着读" in description
        parameters = function["parameters"]
        assert parameters["required"] == ["url"]
        properties = parameters["properties"]
        assert properties["url"]["type"] == "string"
        assert "http://" in properties["url"]["description"]
        assert "https://" in properties["url"]["description"]
        assert properties["start_index"]["type"] == "integer"
        assert "默认 0" in properties["start_index"]["description"]


class TestToolCacheWebFields:
    def test_web_fields_default_empty(self) -> None:
        """Task 6 的缓存 / 计数都挂在这几个字段上，且全部有默认值（不影响既有构造）。"""
        cache = ToolCache()
        assert cache.searches == {}
        assert cache.pages == {}
        assert cache.seen_urls == set()
        assert cache.robots == {}
        assert cache.web_calls == 0

    def test_existing_fields_still_default(self) -> None:
        cache = ToolCache()
        assert cache.clues == {}
        assert cache.image_clues == {}
        assert cache.image_generations == {}

    def test_fields_are_not_shared_between_instances(self) -> None:
        """可变默认值必须走 `default_factory`：两个实例不能共用同一个 dict / set。"""
        first, second = ToolCache(), ToolCache()
        first.searches["热点"] = []
        first.seen_urls.add("https://example.test/a")
        first.pages[("https://example.test/a", 0)] = {}
        first.robots["example.test"] = object()
        first.web_calls += 1
        assert second.searches == {}
        assert second.seen_urls == set()
        assert second.pages == {}
        assert second.robots == {}
        assert second.web_calls == 0

# ---------- Task 6：handler 行为 ----------

CONTRACT = Path(PROJECT_ROOT) / "docs" / "contracts" / "数据契约.md"


def make_ctx(**web: Any) -> ToolContext:
    """离线工具上下文：只带配置与一张轮内缓存，session 用 None（handler 不碰库）。"""
    return ToolContext(cfg=make_cfg(**web), session=cast(Any, None))


def hit(url: str, *, title: str = "一条热点", snippet: str = "摘要",
        published: str | None = None) -> SearchHit:
    return SearchHit(title=title, url=url, snippet=snippet, published_date=published)


class FakeSearch:
    """假搜索适配器：记录入参，按预设返回或抛错（handler 不该认识 httpx）。"""

    def __init__(
        self,
        hits: list[SearchHit] | None = None,
        unresponsive: list[list[str]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.hits = list(hits or [])
        self.unresponsive = [list(item) for item in (unresponsive or [])]
        self.error = error

    async def __call__(self, client: Any, cfg: Any, query: str, *,
                       count: int) -> SearchResult:
        self.calls.append({"client": client, "query": query, "count": count})
        if self.error is not None:
            raise self.error
        return SearchResult(hits=list(self.hits), unresponsive=self.unresponsive)


def make_page(**overrides: Any) -> FetchedPage:
    values: dict[str, Any] = {
        "final_url": "https://example.test/a",
        "title": "示例标题",
        "text": "正文" * 120,
        "char_count": 240,
        "truncated": False,
        "next_start_index": None,
    }
    values.update(overrides)
    return FetchedPage(**values)


class FakeFetch:
    """假抓取适配器：把 `autonomous` / `start_index` 这些**接线参数**原样留证。"""

    def __init__(self, page: FetchedPage | None = None,
                 error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.page = page or make_page()
        self.error = error

    async def __call__(
        self,
        client: Any,
        cfg: Any,
        url: str,
        *,
        autonomous: bool,
        start_index: int = 0,
        max_length: int | None = None,
        robots_cache: dict[str, Any] | None = None,
    ) -> FetchedPage:
        self.calls.append(
            {"client": client, "url": url, "autonomous": autonomous,
             "start_index": start_index, "max_length": max_length,
             "robots_cache": robots_cache}
        )
        if self.error is not None:
            raise self.error
        return self.page


def contract_fields(tool: str) -> tuple[set[str], set[str]]:
    """从《数据契约》登记表读回（必定出现的字段，缓存命中追加字段），整行匹配。"""
    for line in CONTRACT.read_text(encoding="utf-8").splitlines():
        if not line.startswith(f"| `{tool}` |"):
            continue
        columns = line.split("|")
        return (
            set(re.findall(r"`([^`]+)`", columns[2])),
            set(re.findall(r"`([^`]+)`", columns[3])),
        )
    raise AssertionError(f"《数据契约》没有登记 {tool} 的 payload 形状")


class TestSearchHotspotsHandler:
    @pytest.mark.asyncio
    async def test_search_hotspots_rejects_empty_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空 query 不能发请求：文案要说清「传什么」，不是「参数错误」。"""
        search = FakeSearch([hit("https://example.test/a")])
        monkeypatch.setattr(chat_tools, "search_web", search)
        outcome = await chat_tools.search_hotspots(make_ctx(enabled=True), {"query": "   "})
        assert outcome.status == "failed"
        assert "query" in outcome.error
        assert "80" in outcome.error
        assert search.calls == []

    @pytest.mark.asyncio
    async def test_search_payload_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        search = FakeSearch([hit("https://example.test/a", title="露营", snippet="摘要",
                                 published="2026-10-18")])
        monkeypatch.setattr(chat_tools, "search_web", search)
        outcome = await chat_tools.search_hotspots(
            make_ctx(enabled=True), {"query": "最近的露营热点", "count": 3}
        )
        assert outcome.status == "succeeded"
        assert set(outcome.payload) == {"query", "count", "results", "note"}
        assert outcome.payload["query"] == "最近的露营热点"
        assert outcome.payload["count"] == 1
        assert outcome.payload["results"] == [
            {"title": "露营", "url": "https://example.test/a", "snippet": "摘要",
             "published_date": "2026-10-18"}
        ]
        assert search.calls[0]["client"] is None   # 不借共享 client（交接坑 3）
        assert search.calls[0]["count"] == 3

    @pytest.mark.asyncio
    async def test_unresponsive_engines_lands_in_note(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0 条命中时唯一的健康信号在 unresponsive：必须写进给人看的 note。"""
        search = FakeSearch([], unresponsive=[["google", "timeout"]])
        monkeypatch.setattr(chat_tools, "search_web", search)
        outcome = await chat_tools.search_hotspots(make_ctx(enabled=True), {"query": "露营"})
        assert outcome.status == "succeeded"
        note = outcome.payload["note"]
        assert "google" in note and "timeout" in note
        assert "结果可能不完整" in note

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_increment_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缓存命中不增加 `web_calls`：同一 query 本轮只付一次。"""
        search = FakeSearch([hit("https://example.test/a")])
        monkeypatch.setattr(chat_tools, "search_web", search)
        ctx = make_ctx(enabled=True)
        first = await chat_tools.search_hotspots(ctx, {"query": "露营"})
        second = await chat_tools.search_hotspots(ctx, {"query": "露营"})
        assert len(search.calls) == 1
        assert ctx.cache.web_calls == 1
        assert "cached" not in first.payload
        assert second.payload["cached"] is True
        assert second.payload["results"] == first.payload["results"]


class TestFetchUrlHandler:
    @pytest.mark.asyncio
    async def test_fetch_url_rejects_non_http_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fetch = FakeFetch()
        monkeypatch.setattr(chat_tools, "fetch_readable", fetch)
        ctx = make_ctx(enabled=True)
        for bad in ("", "   ", "file:///etc/passwd", "ftp://example.test/a", "example.test/a"):
            outcome = await chat_tools.fetch_url(ctx, {"url": bad})
            assert outcome.status == "failed", bad
            assert "http" in outcome.error, bad
        assert fetch.calls == []

    @pytest.mark.asyncio
    async def test_fetch_payload_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = make_page(final_url="https://example.test/final", title="标题",
                         text="正文段", char_count=900, truncated=True,
                         next_start_index=20000)
        fetch = FakeFetch(page)
        monkeypatch.setattr(chat_tools, "fetch_readable", fetch)
        outcome = await chat_tools.fetch_url(
            make_ctx(enabled=True), {"url": "https://example.test/a", "start_index": 20000}
        )
        assert outcome.status == "succeeded"
        assert outcome.payload == {
            "url": "https://example.test/a",
            "final_url": "https://example.test/final",
            "title": "标题",
            "text": "正文段",
            "char_count": 900,
            "truncated": True,
            "next_start_index": 20000,
        }
        assert fetch.calls[0]["start_index"] == 20000
        assert fetch.calls[0]["client"] is None

    @pytest.mark.asyncio
    async def test_search_records_seen_urls_then_fetch_is_autonomous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """关键接线：搜到的 url 记进 `seen_urls`，再抓它就是自主调用（换 UA + 走 robots）。"""
        search = FakeSearch([hit("https://example.test/a"), hit("https://example.test/b")])
        fetch = FakeFetch()
        monkeypatch.setattr(chat_tools, "search_web", search)
        monkeypatch.setattr(chat_tools, "fetch_readable", fetch)
        ctx = make_ctx(enabled=True)
        await chat_tools.search_hotspots(ctx, {"query": "露营"})
        assert ctx.cache.seen_urls == {"https://example.test/a", "https://example.test/b"}
        await chat_tools.fetch_url(ctx, {"url": "https://example.test/a"})
        await chat_tools.fetch_url(ctx, {"url": "https://user-pasted.test/x"})
        assert [call["autonomous"] for call in fetch.calls] == [True, False]
        # robots 缓存就是这一轮同一份 dict（Task 5 定的形状）
        assert fetch.calls[0]["robots_cache"] is ctx.cache.robots

    @pytest.mark.asyncio
    async def test_fetch_page_cache_key_includes_start_index(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缓存键是 (url, start_index)：只用 url 会让第二页永远读到第一页。"""
        fetch = FakeFetch(make_page(text="第一段"))
        monkeypatch.setattr(chat_tools, "fetch_readable", fetch)
        ctx = make_ctx(enabled=True)
        first = await chat_tools.fetch_url(ctx, {"url": "https://example.test/a"})
        second = await chat_tools.fetch_url(
            ctx, {"url": "https://example.test/a", "start_index": 20000}
        )
        assert len(fetch.calls) == 2          # 续读是一次新的 web_calls
        assert ctx.cache.web_calls == 2
        assert "cached" not in second.payload
        again = await chat_tools.fetch_url(
            ctx, {"url": "https://example.test/a", "start_index": 20000}
        )
        assert len(fetch.calls) == 2          # 同一页同一位置才命中
        assert ctx.cache.web_calls == 2
        assert again.payload["cached"] is True
        assert again.payload["text"] == second.payload["text"]
        assert first.payload["text"] == "第一段"


class TestWebToolFailurePaths:
    @pytest.mark.asyncio
    async def test_turn_limit_returns_failed_with_actionable_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配额用完是 failed + 一句能直接用的话，且不再发请求。"""
        search = FakeSearch([hit("https://example.test/a")])
        fetch = FakeFetch()
        monkeypatch.setattr(chat_tools, "search_web", search)
        monkeypatch.setattr(chat_tools, "fetch_readable", fetch)
        ctx = make_ctx(enabled=True)
        ctx.cache.web_calls = ctx.cfg.web.max_calls_per_turn
        searched = await chat_tools.search_hotspots(ctx, {"query": "露营"})
        fetched = await chat_tools.fetch_url(ctx, {"url": "https://example.test/a"})
        for outcome in (searched, fetched):
            assert outcome.status == "failed"
            assert "本轮网页工具已用完" in outcome.error
        assert search.calls == [] and fetch.calls == []
        assert ctx.cache.web_calls == ctx.cfg.web.max_calls_per_turn

    @pytest.mark.asyncio
    async def test_adapter_error_maps_to_failed_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """适配器抛错一律映射成 failed：抛出会触发节点重试，重复付费。"""
        monkeypatch.setattr(
            chat_tools, "search_web", FakeSearch(error=WebSearchError("timeout", "15s"))
        )
        monkeypatch.setattr(
            chat_tools, "fetch_readable",
            FakeFetch(error=WebFetchError("robots", "Disallow: /")),
        )
        ctx = make_ctx(enabled=True)
        searched = await chat_tools.search_hotspots(ctx, {"query": "露营"})
        fetched = await chat_tools.fetch_url(ctx, {"url": "https://example.test/a"})
        assert searched.status == "failed" and "超时" in searched.error
        assert fetched.status == "failed" and "robots.txt" in fetched.error
        assert ctx.cache.web_calls == 2        # 请求已经发出去了，配额照记


class TestWebToolContract:
    @pytest.mark.asyncio
    async def test_payload_matches_data_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """payload 的键集合与《数据契约》登记的字段逐项相等（契约文本是权威）。"""
        monkeypatch.setattr(
            chat_tools, "search_web", FakeSearch([hit("https://example.test/a")])
        )
        monkeypatch.setattr(chat_tools, "fetch_readable", FakeFetch())
        ctx = make_ctx(enabled=True)

        search_required, search_cached = contract_fields(TOOL_SEARCH_HOTSPOTS)
        searched = await chat_tools.search_hotspots(ctx, {"query": "露营"})
        assert set(searched.payload) == search_required
        assert search_cached == {"cached"}
        again = await chat_tools.search_hotspots(ctx, {"query": "露营"})
        assert set(again.payload) == search_required | search_cached

        fetch_required, fetch_cached = contract_fields(TOOL_FETCH_URL)
        fetched = await chat_tools.fetch_url(ctx, {"url": "https://example.test/a"})
        assert set(fetched.payload) == fetch_required
        assert fetch_cached == {"cached"}
        refetched = await chat_tools.fetch_url(ctx, {"url": "https://example.test/a"})
        assert set(refetched.payload) == fetch_required | fetch_cached

    def test_web_handlers_are_registered(self) -> None:
        """分发表里有这两条：Task 7 的图按名字查 HANDLERS，查不到就永远调不动。"""
        assert HANDLERS[TOOL_SEARCH_HOTSPOTS] is chat_tools.search_hotspots
        assert HANDLERS[TOOL_FETCH_URL] is chat_tools.fetch_url

class TestWebToolUnexpectedAdapterErrors:
    """审查 I1：外层兜底 `except Exception` 是「绝不抛异常」在**非适配器异常**上的唯一实现。

    dispatcher 对这些工具调用**不设 try**（`chat_turns.py` 只记 span 再 re-raise），
    异常一旦逃逸就打到 `RetryPolicy(max_attempts=3)` 上重复付费——正是设计要防的事。
    """

    @pytest.mark.asyncio
    async def test_unexpected_search_error_maps_to_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            chat_tools, "search_web", FakeSearch(error=RuntimeError("boom"))
        )
        ctx = make_ctx(enabled=True)
        outcome = await chat_tools.search_hotspots(ctx, {"query": "露营"})
        assert outcome.status == "failed"
        assert "搜索失败" in outcome.error
        assert ctx.cache.web_calls == 1        # 请求已经发出去了，配额照记
        assert "露营" not in outcome.error      # 错误文案不带用户原文

    @pytest.mark.asyncio
    async def test_unexpected_fetch_error_maps_to_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            chat_tools, "fetch_readable", FakeFetch(error=RuntimeError("boom"))
        )
        ctx = make_ctx(enabled=True)
        outcome = await chat_tools.fetch_url(ctx, {"url": "https://example.test/a"})
        assert outcome.status == "failed"
        assert "读取网页失败" in outcome.error
        assert ctx.cache.web_calls == 1
        assert outcome.payload == {}           # 失败不交半份 payload


class TestWebToolArgumentBounds:
    """审查 I1：参数边界分支（越界 / 负数）也要钉住，文案必须给出下一步。"""

    @pytest.mark.asyncio
    async def test_search_hotspots_rejects_out_of_range_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        search = FakeSearch([hit("https://example.test/a")])
        monkeypatch.setattr(chat_tools, "search_web", search)
        ctx = make_ctx(enabled=True)
        for bad in (0, 11, "很多"):
            outcome = await chat_tools.search_hotspots(ctx, {"query": "露营", "count": bad})
            assert outcome.status == "failed", bad
            assert "count" in outcome.error and "10" in outcome.error, bad
        assert search.calls == []

    @pytest.mark.asyncio
    async def test_fetch_url_rejects_negative_start_index(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = FakeFetch()
        monkeypatch.setattr(chat_tools, "fetch_readable", fetch)
        ctx = make_ctx(enabled=True)
        outcome = await chat_tools.fetch_url(
            ctx, {"url": "https://example.test/a", "start_index": -1}
        )
        assert outcome.status == "failed"
        assert "start_index" in outcome.error
        assert "0" in outcome.error
        assert fetch.calls == []
