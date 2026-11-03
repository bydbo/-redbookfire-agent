"""`tools/webreader.py`（网页正文适配器）的契约测试。

只走 `httpx.MockTransport`：不联网、不读本机 DNS、不依赖本机 hosts 文件。
默认把任何域名解析到一个公网 IP（`fake_dns` 夹具），用例要测 SSRF 拦截时往映射里
塞内网地址即可。抓取响应用**流式**响应（`ChunkStream`）构造：`httpx.Response(content=b"")`
在 MockTransport 下已被标记为 consumed，`aiter_raw()` 会直接 StreamConsumed。

用例钉住的三件「反直觉」事实（trafilatura 2.3.1 实测，见任务报告）：
1. JS 空壳页在默认参数下返回的是 `<noscript>` 里的文字，只有 `favor_precision=True`
   才落 `None`——所以「抽不到正文」必须 `is None` + 长度校验双保险；
2. 库没有最小长度门槛（30 字照返），「<200 字符判失败」是本项目自己的策略阈值；
3. `extract_metadata(...).title` 取不到时是 `None`（不是空串），且它的 URL 参数叫
   `default_url`（2.3.1 没有 `url=`）。
"""

from __future__ import annotations

import gzip
import socket
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from xhs_agent.config import AppConfig, WebConfig
from xhs_agent.tools import webreader
from xhs_agent.tools.webreader import WebFetchError, fetch_readable

PAGE_URL = "https://example.com/article"
# 任意公网地址都行：MockTransport 不会真连它，这里只用来喂 SSRF 判定
PUBLIC_IP = "93.184.216.34"

# httpx.MockTransport 的处理函数签名
Handler = Callable[[httpx.Request], httpx.Response]

PARAGRAPH = "羽毛球场地预订的注意事项与价格说明，本文按场馆逐条列出。"


class ChunkStream(httpx.AsyncByteStream):
    """可控分块流：记录吐出了几块，用来证明超限时提前中止、没有读完整个响应。"""

    def __init__(self, chunk: bytes, count: int) -> None:
        self._chunk = chunk
        self._count = count
        self.emitted = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._count):
            self.emitted += 1
            yield self._chunk


def make_cfg(**overrides: Any) -> AppConfig:
    """默认 AppConfig + 关掉 robots 的 [web] 段：只有 robots 用例自己打开。"""
    cfg = AppConfig()
    cfg.web = WebConfig(**{"check_robots_txt": False, **overrides})
    return cfg


def article_html(title: str = "羽毛球场地预订指南", chars: int = 900) -> str:
    """一段能被 trafilatura 真抽出来的正文；`chars` 控制正文规模。"""
    body = (PARAGRAPH * (chars // len(PARAGRAPH) + 1))[:chars]
    return (f"<html><head><title>{title}</title></head><body>"
            f"<article><p>{body}</p></article></body></html>")


def html_response(html: str | bytes, *, status: int = 200,
                  headers: dict[str, str] | None = None,
                  chunks: int = 1) -> httpx.Response:
    """流式 text/html 响应；`headers` 可覆盖 content-type 或追加 content-length 等。"""
    merged = {"content-type": "text/html; charset=utf-8"}
    merged.update(headers or {})
    body = html.encode("utf-8") if isinstance(html, str) else html
    return httpx.Response(status, stream=ChunkStream(body, chunks), headers=merged)


def client_for(handler: Handler,
               calls: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    """把处理器包成带 MockTransport 的客户端，顺带记录每个请求。"""

    def wrapped(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


def deny_all_robots() -> httpx.Response:
    return httpx.Response(200, text="User-agent: *\nDisallow: /\n",
                          headers={"content-type": "text/plain"})


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """默认把所有域名解析到同一个公网 IP，用例不联网、不依赖本机 DNS 与 hosts。

    返回值是 host → IP 的可写映射：用例往里塞内网地址即可触发 SSRF 拦截。
    """
    table: dict[str, str] = {}

    def fake_getaddrinfo(host: str, port: int | None, family: int = 0,
                         socktype: int = 0, proto: int = 0,
                         flags: int = 0) -> list[tuple[Any, ...]]:
        ip = table.get(host, PUBLIC_IP)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return table


async def fetch(client: httpx.AsyncClient, cfg: AppConfig | None = None,
                url: str = PAGE_URL, **kwargs: Any) -> Any:
    """跑一次 `fetch_readable` 并在结束时关掉客户端。"""
    try:
        return await fetch_readable(client, cfg or make_cfg(), url, **kwargs)
    finally:
        await client.aclose()


class TestScheme:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_url", ["file:///etc/passwd",
                                         "ftp://example.com/a.html"])
    async def test_rejects_non_http_scheme(self, bad_url: str) -> None:
        calls: list[httpx.Request] = []
        client = client_for(lambda _request: html_response(article_html()), calls)
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, url=bad_url, autonomous=False)
        assert excinfo.value.reason == "scheme"
        assert calls == []


class TestPrivateGuard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("category", "ip"), [
        ("loopback", "127.0.0.1"),
        ("private", "10.1.2.3"),
        ("link_local", "169.254.10.10"),
        ("multicast", "224.0.0.1"),
        ("reserved", "240.0.0.1"),
        ("unspecified", "0.0.0.0"),
    ])
    async def test_blocks_private_destinations(self, fake_dns: dict[str, str],
                                               category: str,
                                               ip: str) -> None:
        fake_dns["internal.test"] = ip
        calls: list[httpx.Request] = []
        client = client_for(lambda _request: html_response(article_html()), calls)
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, url="http://internal.test/a", autonomous=False)
        assert excinfo.value.reason == "blocked_host", category
        assert calls == [], category          # 连请求都没发出去

    @pytest.mark.asyncio
    async def test_blocks_private_host_on_redirect_hop(
            self, fake_dns: dict[str, str]) -> None:
        fake_dns["internal.test"] = "10.0.0.5"
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/a":
                return httpx.Response(
                    302, headers={"location": "http://internal.test/secret"})
            return html_response(article_html())

        client = client_for(handler, calls)
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, url="http://public.test/a", autonomous=False)
        assert excinfo.value.reason == "blocked_host"
        # 第二跳被钩子拦下：transport 只看得见第一跳
        assert [str(r.url) for r in calls] == ["http://public.test/a"]

    @pytest.mark.asyncio
    async def test_allow_private_ips_true_bypasses_guard(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
            raise AssertionError("allow_private_ips=True 时不该做 DNS 校验")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        client = client_for(lambda _request: html_response(article_html()))
        page = await fetch(client, cfg=make_cfg(allow_private_ips=True),
                           url="http://127.0.0.1/a", autonomous=False)
        assert page.char_count >= 200

    @pytest.mark.asyncio
    async def test_dns_failure_maps_to_dns(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raiser(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", raiser)
        client = client_for(lambda _request: html_response(article_html()))
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, url="https://nowhere.test/a", autonomous=False)
        assert excinfo.value.reason == "dns"

    @pytest.mark.asyncio
    async def test_guard_does_not_rewrite_request_url(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """钩子只做拒绝判定：不钉 IP、不改写 request.url（已定设计：不防 DNS rebinding）。"""
        looked_up: list[str] = []

        def spy(host: str, port: int | None, family: int = 0,
                socktype: int = 0, proto: int = 0,
                flags: int = 0) -> list[tuple[Any, ...]]:
            looked_up.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP,
                                                                 port or 80))]

        monkeypatch.setattr(socket, "getaddrinfo", spy)
        calls: list[httpx.Request] = []
        client = client_for(lambda _request: html_response(article_html()), calls)
        page = await fetch(client, autonomous=False)
        assert looked_up == ["example.com"]                # 钩子确实跑了
        assert [str(r.url) for r in calls] == [PAGE_URL]
        assert calls[0].headers["host"] == "example.com"   # 域名没被换成 IP
        assert page.final_url == PAGE_URL

    @pytest.mark.asyncio
    async def test_redirect_loop_maps_to_http(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            hop = int(request.url.params.get("n", "0"))
            return httpx.Response(
                302, headers={"location": f"https://example.com/article?n={hop + 1}"})

        client = client_for(handler)
        with pytest.raises(WebFetchError) as excinfo:
            # fetch_max_redirects 真的被用上了：默认 20 次在这里会转很多圈
            await fetch(client, cfg=make_cfg(fetch_max_redirects=2),
                        autonomous=False)
        assert excinfo.value.reason == "http"

    @pytest.mark.asyncio
    async def test_builds_and_closes_own_client_when_none(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        created: list[httpx.AsyncClient] = []

        def factory(timeout_s: float) -> httpx.AsyncClient:
            client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _request: html_response(article_html())))
            created.append(client)
            return client

        monkeypatch.setattr(webreader, "build_http_client", factory)
        page = await fetch_readable(None, make_cfg(), PAGE_URL, autonomous=False)
        assert page.char_count >= 200
        assert len(created) == 1
        assert created[0].is_closed


class TestUserAgent:
    @pytest.mark.asyncio
    async def test_autonomous_uses_autonomous_ua(self) -> None:
        calls: list[httpx.Request] = []
        client = client_for(lambda _request: html_response(article_html()), calls)
        await fetch(client, cfg=make_cfg(user_agent_autonomous="auto-ua/9",
                                        user_agent_manual="manual-ua/9"),
                    autonomous=True)
        assert calls[-1].headers["user-agent"] == "auto-ua/9"

    @pytest.mark.asyncio
    async def test_manual_uses_manual_ua(self) -> None:
        calls: list[httpx.Request] = []
        client = client_for(lambda _request: html_response(article_html()), calls)
        await fetch(client, cfg=make_cfg(user_agent_autonomous="auto-ua/9",
                                        user_agent_manual="manual-ua/9"),
                    autonomous=False)
        assert calls[-1].headers["user-agent"] == "manual-ua/9"


class TestRobots:
    @pytest.mark.asyncio
    async def test_robots_denied_blocks_autonomous(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(
                    200, text="User-agent: *\nDisallow: /private\n",
                    headers={"content-type": "text/plain"})
            return html_response(article_html())

        client = client_for(handler, calls)
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, cfg=make_cfg(check_robots_txt=True),
                        url="https://example.com/private/x", autonomous=True)
        assert excinfo.value.reason == "robots"
        assert [r.url.path for r in calls] == ["/robots.txt"]   # 正文请求没发出去

    @pytest.mark.asyncio
    async def test_robots_5xx_treated_unreachable(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(503, text="busy")
            return html_response(article_html())

        client = client_for(handler, calls)
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, cfg=make_cfg(check_robots_txt=True),
                        autonomous=True)
        assert excinfo.value.reason == "robots"
        assert [r.url.path for r in calls] == ["/robots.txt"]

    @pytest.mark.asyncio
    async def test_robots_4xx_allows(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404, text="not found")
            return html_response(article_html())

        client = client_for(handler, calls)
        page = await fetch(client, cfg=make_cfg(check_robots_txt=True),
                           autonomous=True)
        assert page.char_count >= 200
        assert [r.url.path for r in calls] == ["/robots.txt", "/article"]

    @pytest.mark.asyncio
    async def test_robots_skipped_for_manual(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return deny_all_robots()
            return html_response(article_html())

        client = client_for(handler, calls)
        page = await fetch(client, cfg=make_cfg(check_robots_txt=True),
                           autonomous=False)
        assert page.char_count >= 200
        assert [r.url.path for r in calls] == ["/article"]

    @pytest.mark.asyncio
    async def test_robots_cached_per_host(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n",
                                      headers={"content-type": "text/plain"})
            return html_response(article_html())

        client = client_for(handler, calls)
        cache: dict[str, Any] = {}
        cfg = make_cfg(check_robots_txt=True)
        try:
            first = await fetch_readable(client, cfg, "https://example.com/a",
                                         autonomous=True, robots_cache=cache)
            second = await fetch_readable(client, cfg, "https://example.com/b",
                                          autonomous=True, robots_cache=cache)
        finally:
            await client.aclose()
        assert first.char_count >= 200 and second.char_count >= 200
        assert [r.url.path for r in calls].count("/robots.txt") == 1
        assert len(cache) == 1


class TestContentType:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("media_type", ["application/pdf", "image/png"])
    async def test_rejects_non_html_content_type(self, media_type: str) -> None:
        client = client_for(lambda _request: html_response(
            b"%PDF-1.7 not html", headers={"content-type": media_type}))
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, autonomous=False)
        assert excinfo.value.reason == "content_type"


class TestSizeAndEncoding:
    @pytest.mark.asyncio
    async def test_content_length_over_limit_maps_to_too_large(self) -> None:
        client = client_for(lambda _request: html_response(
            article_html(), headers={"content-length": "9000000"}))
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, cfg=make_cfg(fetch_max_bytes=65536),
                        autonomous=False)
        assert excinfo.value.reason == "too_large"

    @pytest.mark.asyncio
    async def test_raw_bytes_over_limit_aborts_download(self) -> None:
        client = client_for(lambda _request: html_response(
            b"x" * 1024, chunks=200))            # 200 KiB，上限 64 KiB
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, cfg=make_cfg(fetch_max_bytes=65536),
                        autonomous=False)
        assert excinfo.value.reason == "too_large"

    @pytest.mark.asyncio
    async def test_aborts_before_reading_whole_body(self) -> None:
        stream = ChunkStream(b"x" * 1024, 200)
        client = client_for(lambda _request: httpx.Response(
            200, stream=stream, headers={"content-type": "text/html"}))
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, cfg=make_cfg(fetch_max_bytes=65536),
                        autonomous=False)
        assert excinfo.value.reason == "too_large"
        # 没有 content-length 时只能边读边数：超限即停，没读完 200 块
        assert stream.emitted < 200

    @pytest.mark.asyncio
    async def test_gzip_bomb_is_capped(self) -> None:
        bomb = gzip.compress(b"\x00" * (4 * 1024 * 1024))
        assert len(bomb) < 65536         # 压缩后只有 4KB 出头，原始字节闸拦不住
        client = client_for(lambda _request: html_response(
            bomb, headers={"content-encoding": "gzip"}))
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, cfg=make_cfg(fetch_max_bytes=65536),
                        autonomous=False)
        assert excinfo.value.reason == "too_large"

    @pytest.mark.asyncio
    async def test_gzip_encoded_page_is_decoded(self) -> None:
        # 服务器无视 Accept-Encoding: identity 时，gzip 必须能正常解出来（不是直接拒）
        payload = gzip.compress(article_html().encode("utf-8"))
        client = client_for(lambda _request: html_response(
            payload, headers={"content-encoding": "gzip"}))
        page = await fetch(client, autonomous=False)
        assert page.char_count >= 200

    @pytest.mark.asyncio
    async def test_broken_gzip_maps_to_content_type(self) -> None:
        client = client_for(lambda _request: html_response(
            b"not gzip at all", headers={"content-encoding": "gzip"}))
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, autonomous=False)
        assert excinfo.value.reason == "content_type"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("encoding", ["br", "zstd"])
    async def test_unsupported_content_encoding_rejected(self,
                                                         encoding: str) -> None:
        # 请求头已经要求 identity；仍被强行压缩时不许自己猜解码器
        client = client_for(lambda _request: html_response(
            b"whatever", headers={"content-encoding": encoding}))
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, autonomous=False)
        assert excinfo.value.reason == "content_type"


class TestPagination:
    @pytest.mark.asyncio
    async def test_start_index_slices_and_sets_next(self) -> None:
        client = client_for(lambda _request: html_response(article_html()))
        full = await fetch_readable(client, make_cfg(), PAGE_URL, autonomous=False)
        try:
            page = await fetch_readable(client, make_cfg(), PAGE_URL,
                                        autonomous=False, start_index=100,
                                        max_length=150)
        finally:
            await client.aclose()
        assert page.char_count == full.char_count
        assert page.text == full.text[100:250]
        assert page.truncated is True
        assert page.next_start_index == 250

    @pytest.mark.asyncio
    async def test_start_index_beyond_end_returns_empty_text(self) -> None:
        client = client_for(lambda _request: html_response(article_html()))
        full = await fetch_readable(client, make_cfg(), PAGE_URL, autonomous=False)
        try:
            page = await fetch_readable(client, make_cfg(), PAGE_URL,
                                        autonomous=False,
                                        start_index=full.char_count + 50)
        finally:
            await client.aclose()
        assert page.text == ""
        assert page.truncated is False
        assert page.next_start_index is None
        assert page.char_count == full.char_count   # 越界不改变自身长度

    @pytest.mark.asyncio
    async def test_negative_start_index_is_clamped_or_rejected(self) -> None:
        client = client_for(lambda _request: html_response(article_html()))
        full = await fetch_readable(client, make_cfg(), PAGE_URL, autonomous=False)
        try:
            page = await fetch_readable(client, make_cfg(), PAGE_URL,
                                        autonomous=False, start_index=-5)
        finally:
            await client.aclose()
        assert page.text == full.text            # 钳到 0：等价于从头读
        assert page.next_start_index is None

    @pytest.mark.asyncio
    async def test_whole_page_marks_not_truncated(self) -> None:
        client = client_for(lambda _request: html_response(article_html()))
        page = await fetch(client, autonomous=False)
        assert page.truncated is False
        assert page.next_start_index is None
        assert page.char_count == len(page.text)


class TestExtraction:
    @pytest.mark.asyncio
    async def test_extract_none_maps_to_insufficient_text(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(webreader.trafilatura, "extract",
                            lambda *_args, **_kwargs: None)
        client = client_for(lambda _request: html_response(article_html()))
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, autonomous=False)
        assert excinfo.value.reason == "insufficient_text"

    @pytest.mark.asyncio
    async def test_noscript_page_maps_to_insufficient_text(self) -> None:
        # 实测：JS 空壳页在 favor_precision=True 下 extract 返回 None
        shell = ("<html><head><title>Loading</title></head><body>"
                 "<noscript>Please enable JavaScript to view this site.</noscript>"
                 "<div id='root'></div></body></html>")
        client = client_for(lambda _request: html_response(shell))
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, autonomous=False)
        assert excinfo.value.reason == "insufficient_text"

    @pytest.mark.asyncio
    async def test_short_text_below_200_chars_maps_to_insufficient_text(
            self) -> None:
        # 实测：库没有最小长度门槛（30 字照返），200 字符是本项目自己的策略阈值
        client = client_for(lambda _request: html_response(article_html(chars=30)))
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, autonomous=False)
        assert excinfo.value.reason == "insufficient_text"


class TestTitle:
    @pytest.mark.asyncio
    async def test_title_from_metadata(self) -> None:
        client = client_for(lambda _request: html_response(article_html()))
        page = await fetch(client, autonomous=False)
        assert page.title == "羽毛球场地预订指南"

    @pytest.mark.asyncio
    async def test_title_falls_back_to_regex_when_metadata_title_is_none(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 实测：extract_metadata 取不到标题时 title 是 None（不是空串）
        monkeypatch.setattr(webreader.trafilatura, "extract_metadata",
                            lambda *_args, **_kwargs: SimpleNamespace(title=None))
        client = client_for(lambda _request: html_response(
            article_html(title="正则兜底标题")))
        page = await fetch(client, autonomous=False)
        assert page.title == "正则兜底标题"


class TestLibraryBoundaries:
    @pytest.mark.asyncio
    async def test_never_calls_trafilatura_fetch_url_nor_fetch_response(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("不得使用 trafilatura 自带的抓取函数")

        # 两者都会发真网络请求：抓取只能由本模块的 httpx 客户端负责
        monkeypatch.setattr(webreader.trafilatura, "fetch_url", boom)
        monkeypatch.setattr(webreader.trafilatura, "fetch_response", boom)
        client = client_for(lambda _request: html_response(article_html()))
        page = await fetch(client, autonomous=False)
        assert page.char_count >= 200

    @pytest.mark.asyncio
    async def test_timeout_maps_to_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        client = client_for(handler)
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, autonomous=False)
        assert excinfo.value.reason == "timeout"

    @pytest.mark.asyncio
    async def test_http_error_status_maps_to_http(self) -> None:
        client = client_for(lambda _request: html_response("forbidden",
                                                           status=403))
        with pytest.raises(WebFetchError) as excinfo:
            await fetch(client, autonomous=False)
        assert excinfo.value.reason == "http"
