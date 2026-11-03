"""[web] 配置段的契约测试（配置契约 §3.14）。

所有用例只读 tmp_path 下的临时 TOML、只注入假环境变量，不碰真实的 config/；
预检第 8 步用 `httpx.MockTransport` 顶替 SearXNG 实例，不联网。
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from xhs_agent.config import (
    AppConfig,
    ConfigError,
    EnvView,
    WebConfig,
    config_to_dict,
    load_config,
)
from xhs_agent.probe import (
    E_CONFIG_INVALID,
    E_WEB_SEARCH_UNAVAILABLE,
    E_WEB_SEARCH_URL_MISSING,
    PreflightReport,
    check_web,
    preflight,
)

# 契约 §3.14 的 17 个字段与默认值（逐字对齐，改这里就等于改契约）
DEFAULTS = {
    "enabled": False,
    "search_url": "",
    "engines": "",
    "api_key_env": "XHS_WEB_SEARCH_API_KEY",
    "search_timeout_s": 15,
    "max_results": 8,
    "snippet_chars": 240,
    "fetch_enabled": True,
    "fetch_timeout_s": 30,
    "fetch_max_bytes": 2_000_000,
    "fetch_max_chars": 20_000,
    "fetch_max_redirects": 2,
    "check_robots_txt": True,
    "max_calls_per_turn": 3,
    "allow_private_ips": False,
    "user_agent_autonomous": "xhs-agent/1.0 (Autonomous)",
    "user_agent_manual": "xhs-agent/1.0 (User-Specified)",
}

SEARXNG = "http://searxng.test:8888"
WEB_ENV_KEY = "XHS_WEB_SEARCH_API_KEY"


def write_config(tmp_path, body: str = "") -> str:
    """写一份临时 config.toml。

    先关掉 `[embedding].enabled`（默认 true，见契约 §3.3）：否则每个走 `preflight` 的用例
    都得额外带 DASHSCOPE_API_KEY，掩盖各自的测试意图（口径同 tests/unit/test_probe.py）。
    """
    if "[embedding]" not in body:
        body = "[embedding]\nenabled = false\n" + body
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def write_web_config(tmp_path, body: str = "", *, search_url: str = SEARXNG) -> str:
    """写一份 [web] 段已启用的临时 config.toml（第 8 步用例的公共前置）。"""
    return write_config(tmp_path, f'[web]\nenabled = true\nsearch_url = "{search_url}"\n{body}')


def ready_env(**values: str) -> EnvView:
    """第 1–2 步能过的注入环境：用例不读真实 config/.env。"""
    base = {
        "DEEPSEEK_API_KEY": "sk-x",
        "DATABASE_URL": "postgresql+asyncpg://xhs:xhs@localhost:5432/xhs",
        "REDIS_URL": "redis://localhost:6379/0",
    }
    base.update(values)
    return EnvView({}, base)


def transport(status: int, *, calls: list[httpx.Request] | None = None):
    """假 SearXNG：记录请求，返回固定状态码。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, json={"results": []})

    return httpx.MockTransport(handler)


class TestWebDefaults:
    def test_web_defaults(self):
        dumped = WebConfig().model_dump()
        assert len(DEFAULTS) == 17
        assert dumped == DEFAULTS

    def test_app_config_owns_a_web_section(self):
        cfg = AppConfig()
        assert isinstance(cfg.web, WebConfig)
        assert cfg.web.model_dump() == DEFAULTS

    def test_chat_defaults_are_untouched(self):
        # 本任务只加 [web]，不动 [chat] 的任何默认值
        assert AppConfig().chat.max_tool_rounds == 3


class TestWebBounds:
    @pytest.mark.parametrize(
        "field, value",
        [
            ("search_timeout_s", 0),
            ("search_timeout_s", 121),
            ("max_results", 21),
            ("fetch_max_bytes", 1024),
            ("max_calls_per_turn", 11),
        ],
    )
    def test_out_of_range_is_rejected(self, field, value):
        with pytest.raises(ValidationError):
            WebConfig(**{field: value})

    @pytest.mark.parametrize("field", ["user_agent_autonomous", "user_agent_manual"])
    def test_blank_user_agent_is_rejected(self, field):
        with pytest.raises(ValidationError):
            WebConfig(**{field: "   "})

    def test_toml_out_of_range_folds_into_config_error(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config(write_config(tmp_path, "[web]\nmax_results = 21\n"))

    def test_unknown_web_field_is_rejected(self, tmp_path):
        # extra="forbid"：拼错的字段不能被静默忽略
        with pytest.raises(ConfigError):
            load_config(write_config(tmp_path, "[web]\nmax_result = 5\n"))

    def test_api_key_in_toml_is_rejected(self, tmp_path):
        # 密钥不进模型：config.toml 里写 api_key 直接拒绝（契约 §八）
        with pytest.raises(ConfigError):
            load_config(write_config(tmp_path, '[web]\napi_key = "sk-web"\n'))

    def test_web_section_is_accepted_from_toml(self, tmp_path):
        cfg = load_config(write_config(
            tmp_path, '[web]\nenabled = true\nsearch_url = "http://x:8888"\nmax_results = 5\n'))
        assert cfg.web.enabled is True
        assert cfg.web.search_url == "http://x:8888"
        assert cfg.web.max_results == 5
        assert cfg.web.fetch_timeout_s == 30          # 没写的字段回落到代码默认值


class TestWebApiKey:
    def test_api_key_read_from_env(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv(WEB_ENV_KEY, "sk-web-secret")
        cfg = load_config(write_config(tmp_path, "[web]\nenabled = true\n"))
        assert cfg.web.api_key == "sk-web-secret"

    def test_api_key_is_empty_when_env_missing(self, tmp_path):
        cfg = load_config(write_config(tmp_path, "[web]\nenabled = true\n"))
        assert cfg.web.api_key == ""

    def test_api_key_follows_api_key_env(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv("MY_WEB_KEY", "sk-custom")
        cfg = load_config(write_config(tmp_path, '[web]\napi_key_env = "MY_WEB_KEY"\n'))
        assert cfg.web.api_key == "sk-custom"

    def test_api_key_is_not_a_config_field(self):
        # 密钥只从环境变量 / .env 解析，不进配置模型（脱敏摘要与快照里也不该出现）
        assert "api_key" not in WebConfig().model_dump()
        assert "sk-web-secret" not in str(AppConfig().model_dump())

    def test_default_web_config_has_empty_key(self):
        assert WebConfig().api_key == ""


class TestWebDescribe:
    """脱敏摘要要能看出 web 开关状态，但不出现密钥本身（口径同 [llm] 的 has_api_key）。"""

    def test_describe_exposes_web_block(self, tmp_path):
        cfg = load_config(write_web_config(tmp_path))
        block = cfg.describe()["web"]
        assert block["enabled"] is True
        assert block["search_url"] == SEARXNG
        assert block["max_calls_per_turn"] == 3
        assert block["api_key_env"] == WEB_ENV_KEY
        assert block["has_api_key"] is False      # 没配密钥

    def test_describe_reports_key_presence_only(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv(WEB_ENV_KEY, "sk-web-secret")
        cfg = load_config(write_web_config(tmp_path))
        described = cfg.describe()
        assert described["web"]["has_api_key"] is True
        assert "sk-web-secret" not in json.dumps(described, ensure_ascii=False)

    def test_web_block_appears_in_config_snapshot(self, tmp_path):
        # config_to_dict 走 model_dump：web 段作为字段之一必须出现，且不含密钥
        snapshot = config_to_dict(load_config(write_web_config(tmp_path)))
        assert snapshot["web"]["enabled"] is True
        assert "api_key" not in snapshot["web"]

    def test_describe_masks_credentials_in_search_url(self, tmp_path):
        """URL 里内嵌的凭据不能进摘要（脱敏口径同 database_url / redis_url）。"""
        cfg = load_config(write_web_config(
            tmp_path, search_url="http://user:s3cret-pw@searxng.test:8888"))
        described = cfg.describe()
        assert described["web"]["search_url"] == "http://user:***@searxng.test:8888"
        assert "s3cret-pw" not in json.dumps(described, ensure_ascii=False)

    def test_plain_search_url_is_shown_as_is(self, tmp_path):
        cfg = load_config(write_web_config(tmp_path))
        assert cfg.describe()["web"]["search_url"] == SEARXNG


class TestPreflightWebStep:
    """契约 §四 第 8 步：启用 [web] 时的三件事（① 缺 search_url ② JSON 不可用 ③ 无密钥只告警）。"""

    @pytest.mark.asyncio
    async def test_enabled_without_search_url_fails_preflight(self, tmp_path):
        cfg = load_config(write_config(tmp_path, "[web]\nenabled = true\n"))
        problem = await check_web(cfg)
        assert problem is not None
        assert (problem.code, problem.step, problem.kind) == (
            E_WEB_SEARCH_URL_MISSING, 8, "config")
        assert "启用 [web] 后必须配置 search_url（SearXNG 实例地址）" in problem.message
        assert problem.fix                        # 修复提示要能照做
        assert PreflightReport(problems=[problem]).exit_code == 2

    @pytest.mark.asyncio
    async def test_disabled_web_skips_the_probe(self, tmp_path):
        calls: list[httpx.Request] = []
        cfg = load_config(write_config(tmp_path, f'[web]\nsearch_url = "{SEARXNG}"\n'))
        client = httpx.AsyncClient(transport=transport(200, calls=calls))
        try:
            assert await check_web(cfg, client) is None
        finally:
            await client.aclose()
        assert calls == []                        # 关着就一次都不请求

    @pytest.mark.asyncio
    async def test_json_format_unavailable_hints_search_formats(self, tmp_path):
        cfg = load_config(write_web_config(tmp_path))
        client = httpx.AsyncClient(transport=transport(403))
        try:
            problem = await check_web(cfg, client)
        finally:
            await client.aclose()
        assert problem is not None
        assert (problem.code, problem.step, problem.kind) == (
            E_WEB_SEARCH_UNAVAILABLE, 8, "config")
        assert "检查 SearXNG 的 search.formats 是否包含 json" in problem.fix
        assert "403" in problem.message
        assert PreflightReport(problems=[problem]).exit_code == 2

    @pytest.mark.asyncio
    async def test_html_body_with_status_200_also_fails(self, tmp_path):
        """200 但不是 JSON（实例仍只吐 HTML）：同样属 formats 没开，不能误判为通过。"""
        cfg = load_config(write_web_config(tmp_path))
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text="<html></html>")))
        try:
            problem = await check_web(cfg, client)
        finally:
            await client.aclose()
        assert problem is not None
        assert problem.code == E_WEB_SEARCH_UNAVAILABLE
        assert "检查 SearXNG 的 search.formats 是否包含 json" in problem.fix

    @pytest.mark.asyncio
    async def test_healthy_instance_passes_and_probe_shape_is_fixed(self, tmp_path):
        calls: list[httpx.Request] = []
        cfg = load_config(write_web_config(tmp_path))
        client = httpx.AsyncClient(transport=transport(200, calls=calls))
        try:
            assert await check_web(cfg, client) is None
        finally:
            await client.aclose()
        assert len(calls) == 1
        assert str(calls[0].url).startswith(f"{SEARXNG}/search?")
        assert calls[0].url.params["q"] == "test"
        assert calls[0].url.params["format"] == "json"

    @pytest.mark.asyncio
    async def test_trailing_slash_in_search_url_is_normalized(self, tmp_path):
        calls: list[httpx.Request] = []
        cfg = load_config(write_web_config(tmp_path, search_url=f"{SEARXNG}/"))
        client = httpx.AsyncClient(transport=transport(200, calls=calls))
        try:
            assert await check_web(cfg, client) is None
        finally:
            await client.aclose()
        assert str(calls[0].url).startswith(f"{SEARXNG}/search?")

    @pytest.mark.asyncio
    async def test_search_url_without_scheme_is_a_config_problem(self, tmp_path):
        """缺协议头（新手最典型笔误）是配置错误（2），且根本不该发请求。"""
        calls: list[httpx.Request] = []
        cfg = load_config(write_web_config(tmp_path, search_url="127.0.0.1:8888"))
        client = httpx.AsyncClient(transport=transport(200, calls=calls))
        try:
            problem = await check_web(cfg, client)
        finally:
            await client.aclose()
        assert problem is not None
        assert (problem.code, problem.step, problem.kind) == (E_CONFIG_INVALID, 8, "config")
        assert "127.0.0.1:8888" in problem.message
        assert 'http://127.0.0.1:8888' in problem.fix      # 直接给出照抄的写法
        assert PreflightReport(problems=[problem]).exit_code == 2
        assert calls == []                                # 没发请求

    @pytest.mark.asyncio
    async def test_schemeless_url_with_credentials_is_masked(self, tmp_path):
        """非法地址也要脱敏：不能为了排障把 user:pass 打进日志。"""
        cfg = load_config(write_web_config(
            tmp_path, search_url="user:s3cret-pw@searxng.test:8888"))
        client = httpx.AsyncClient(transport=transport(200))
        try:
            problem = await check_web(cfg, client)
        finally:
            await client.aclose()
        assert problem is not None and problem.kind == "config"
        assert "s3cret-pw" not in problem.message and "s3cret-pw" not in problem.fix
        assert "user:***@searxng.test:8888" in problem.message

    @pytest.mark.asyncio
    async def test_wrong_scheme_is_a_config_problem(self, tmp_path):
        calls: list[httpx.Request] = []
        cfg = load_config(write_web_config(tmp_path, search_url="ftp://searxng.test"))
        client = httpx.AsyncClient(transport=transport(200, calls=calls))
        try:
            problem = await check_web(cfg, client)
        finally:
            await client.aclose()
        assert problem is not None and problem.kind == "config"
        assert problem.code == E_CONFIG_INVALID
        assert calls == []

    @pytest.mark.asyncio
    async def test_invalid_port_is_a_config_problem(self, tmp_path):
        """httpx 的 InvalidURL（端口非法）也属配置错误，别落进依赖不可用。"""
        cfg = load_config(write_web_config(tmp_path, search_url="http://searxng.test:notaport"))
        client = httpx.AsyncClient(transport=transport(200))
        try:
            problem = await check_web(cfg, client)
        finally:
            await client.aclose()
        assert problem is not None
        assert (problem.code, problem.kind) == (E_CONFIG_INVALID, "config")
        assert PreflightReport(problems=[problem]).exit_code == 2

    @pytest.mark.asyncio
    async def test_redirect_status_hints_to_https(self, tmp_path):
        """http→https 的 301/302 也是非 200，fix 之外还要在 message 里点明改 https。"""
        cfg = load_config(write_web_config(tmp_path))
        client = httpx.AsyncClient(transport=transport(301))
        try:
            problem = await check_web(cfg, client)
        finally:
            await client.aclose()
        assert problem is not None
        assert "301" in problem.message
        assert "301/302" in problem.message and "https" in problem.message

    @pytest.mark.asyncio
    async def test_problem_text_does_not_leak_url_credentials(self, tmp_path):
        """地址里内嵌的凭据不能出现在预检文案里（§八：密钥不进日志）。"""
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        cfg = load_config(write_web_config(
            tmp_path, search_url="http://user:s3cret-pw@searxng.test:8888"))
        client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        try:
            problem = await check_web(cfg, client)
        finally:
            await client.aclose()
        assert problem is not None
        assert "s3cret-pw" not in problem.message and "s3cret-pw" not in problem.fix
        assert "user:***@" in problem.message

    @pytest.mark.asyncio
    async def test_unreachable_instance_is_a_dependency_problem(self, tmp_path):
        """连不上 / 超时 = 外部依赖不可用（退出码 3），与数据库、Redis 同一口径。"""
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        cfg = load_config(write_web_config(tmp_path))
        client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        try:
            problem = await check_web(cfg, client)
        finally:
            await client.aclose()
        assert problem is not None
        assert (problem.code, problem.step, problem.kind) == (
            E_WEB_SEARCH_UNAVAILABLE, 8, "dependency")
        assert SEARXNG in problem.message         # 连不上也要指明是哪个地址
        assert PreflightReport(problems=[problem]).exit_code == 3

    @pytest.mark.asyncio
    async def test_timeout_is_also_a_dependency_problem(self, tmp_path):
        """超时同属依赖不可用（第 8 步耗时受 search_timeout_s 约束）。"""
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        cfg = load_config(write_web_config(tmp_path))
        client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
        try:
            problem = await check_web(cfg, client)
        finally:
            await client.aclose()
        assert problem is not None and problem.kind == "dependency"

    def test_missing_api_key_only_warns(self, tmp_path):
        # 环境变量由 conftest 的 clean_contract_env 统一清空，无需用例自己 delenv
        cfg = load_config(write_web_config(tmp_path))
        report = preflight(cfg, env=ready_env())
        assert report.ok                          # 无密钥不阻断启动
        assert len(report.warnings) == 1
        assert WEB_ENV_KEY in report.warnings[0]

    def test_present_api_key_is_silent(self, tmp_path):
        cfg = load_config(write_web_config(tmp_path))
        report = preflight(cfg, env=ready_env(**{WEB_ENV_KEY: "sk-web"}))
        assert report.ok and report.warnings == []

    def test_disabled_web_does_not_warn_about_the_key(self, tmp_path):
        cfg = load_config(write_config(tmp_path, "[web]\nenabled = false\n"))
        report = preflight(cfg, env=ready_env())
        assert report.ok and report.warnings == []
