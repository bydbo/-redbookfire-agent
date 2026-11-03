"""网页工具的清单与开关（PR2 · Task 5）：`tools_for` / `web_enabled` / `effective_max_rounds`。

A1 的机器可验证据是 `test_tools_for_disabled_equals_tools`：关闭态下发的清单必须与
`list(TOOLS)` **逐项相等**——多一条就说明默认能力被悄悄改了。本任务只下发清单、不注册
handler（handler 在 Task 6），所以两条网页 schema 放在 `TOOLS` 之外，`tool_names()` 的
无参行为保持全量 = `TOOLS`。

两条 schema 的 description **就是 prompt**（计划逐字钉死）：模型靠它决定该不该调、
不该调到哪。所以这里既钉"有没有"，也钉那几句必含文案。
"""

from __future__ import annotations

from typing import Any

from xhs_agent.config import AppConfig, EnvView, WebConfig
from xhs_agent.services.chat_tools import (
    TOOL_FETCH_URL,
    TOOL_SCHEMA_FETCH_URL,
    TOOL_SCHEMA_SEARCH_HOTSPOTS,
    TOOL_SEARCH_HOTSPOTS,
    TOOLS,
    WEB_MAX_TOOL_ROUNDS,
    ToolCache,
    effective_max_rounds,
    tool_names,
    tools_for,
    web_enabled,
)


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
