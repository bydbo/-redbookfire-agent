"""网页正文适配器：一次 HTTP GET → trafilatura 抽出的 markdown 正文 + 分页游标。

用途：给对话 agent 的抓取工具提供唯一的网页出口；只做「HTTP 请求 + 正文抽取 + 分页
      切片」，不认识工具上下文（`autonomous` 由上层 handler 判好传进来），
      不 import `services/` / `workflows/` / `db/` / `api/`（AGENTS.md §2.0）。
输入：`url` + `autonomous` + 分页参数（`start_index` / `max_length`，后者缺省取
      `fetch_max_chars`）+ `AppConfig`（读 `[web]` 段的 fetch_* 配置）；
      `client` 可注入（单测用 `httpx.MockTransport`），传 None 时按 fetch_timeout_s
      自建一个并在返回前关闭。
输出：`FetchedPage`（`final_url` / `title` / `text` / `char_count` / `truncated` /
      `next_start_index`）；`char_count` 是切片**前**的整篇长度，翻页看最后两个字段。
侧效应：发 1~2 次 HTTP 请求（robots.txt + 正文）；传了 `robots_cache` 时按 host 写缓存；
      不落盘、不写日志、不改全局状态。
错误语义：失败一律抛 `WebFetchError(reason)`，由上层 handler 映射成 ToolOutcome：
      `scheme` / `blocked_host` / `dns` / `robots` / `timeout` / `http` / `too_large` /
      `content_type` / `insufficient_text`。**本模块不重试**：抓取失败要让人看见。

几个与直觉不同的实现决定（都由实测或已定设计固定，别顺手「加固」）：
1. SSRF 钩子必须是 `async def` 且 DNS 走 `asyncio.to_thread`：同步 hook 在 AsyncClient
   下直接 TypeError；同步 getaddrinfo 会阻塞事件循环 370–430ms，所有并发请求一起卡。
2. 钩子只做拒绝判定，**不改写 request.url**（不钉 IP）：本期只防「直连内网」，不防
   DNS rebinding；`test_guard_does_not_rewrite_request_url` 钉着这条设计决定。
3. 字节上限限的是**原始字节**（`aiter_raw()`）：4MiB 的 gzip 炸弹压缩后只有 4098 字节，
   只限解码后字节挡不住它（`aiter_bytes()` 一次迭代就吐 4194304 字节）；gzip 分支再给
   解压结果加同一个上限。
4. `trafilatura.extract()` 同时吃 str 和 bytes（GBK/GB18030 自动探测），不要自己解码；
   `with_metadata=False` 固定——传 True 会让 date 字段凭空捏造；`fetch_url` 与
   `fetch_response` 都会发真网络请求，两者都不用。
5. JS 空壳页在默认参数下返回的是 `<noscript>` 里的文字，只有 `favor_precision=True`
   才落 `None`——所以「抽不到正文」= `is None` 或全空白，再叠一层长度校验双保险。
6. 库没有最小长度门槛（30 字也照返）；「<200 字符判失败」是**本项目的策略阈值**，
   不是 trafilatura 的行为。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import zlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura

from ..config import AppConfig, WebConfig
from .http import build_http_client

# 只抓 http(s)：file:// 之类在 scheme 校验这一步就该拒
SAFE_URL_SCHEMES = ("http", "https")
# 正文抽取只接受这三种；其他（PDF / 图片 / 空 content-type）一律 content_type 失败
HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
IDENTITY_ENCODINGS = frozenset({"", "identity"})
GZIP_ENCODINGS = frozenset({"gzip", "x-gzip"})
# 请求头要求不压缩：省一次解压，也避开 br / zstd 这类没装解码器的编码
ACCEPT_ENCODING = "identity"
ROBOTS_PATH = "/robots.txt"
# 抓取正文的最小字符数：**本项目的策略阈值**，不是 trafilatura 的门槛
MIN_TEXT_CHARS = 200
# 标题兜底：extract_metadata 取不到标题时从原始 HTML 里捞 <title>
_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)

# 六类非公网地址：命中任意一条就拒绝（防 SSRF 直连内网）
_BLOCKED_IP_FLAGS = ("is_private", "is_loopback", "is_link_local", "is_multicast",
                     "is_reserved", "is_unspecified")


@dataclass(frozen=True)
class FetchedPage:
    """一次抓取的结果。

    `char_count` 是切片**前**的整篇正文字数；`next_start_index` 为 None 表示已读完，
    否则把它当下一次的 `start_index` 继续读。
    """

    final_url: str
    title: str
    text: str
    char_count: int
    truncated: bool
    next_start_index: int | None


class WebFetchError(Exception):
    """抓取失败。`reason` ∈ `scheme / blocked_host / dns / robots / timeout / http /
    too_large / content_type / insufficient_text`，上层据此决定提示文案。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


async def fetch_readable(
    client: httpx.AsyncClient | None,
    cfg: AppConfig,
    url: str,
    *,
    autonomous: bool,
    start_index: int = 0,
    max_length: int | None = None,
    robots_cache: dict[str, Any] | None = None,
) -> FetchedPage:
    """抓一个网页并抽出可读正文；失败抛 `WebFetchError`。

    `client` 传 None 时自建（按 fetch_timeout_s）并在返回前关闭。注入的 client 只给单测
    用：本函数会覆盖它的 `follow_redirects` / `max_redirects` / request 钩子，别在多处复用
    同一个实例。`max_length` 缺省取 `fetch_max_chars`。
    """
    web = cfg.web
    target = _validate_target(url)                                   # 1. scheme
    user_agent = (web.user_agent_autonomous if autonomous          # 2. UA
                  else web.user_agent_manual)
    timeout = float(web.fetch_timeout_s)                             # 4. 客户端
    own_client = client is None
    http = client if client is not None else build_http_client(timeout)
    http.follow_redirects = True
    http.max_redirects = int(web.fetch_max_redirects)
    if not web.allow_private_ips:
        http.event_hooks = {**http.event_hooks,
                            "request": [*http.event_hooks.get("request", []),
                                        _reject_private_host]}
    try:
        if autonomous and web.check_robots_txt:                      # 3. robots
            await _check_robots(http, target, user_agent, robots_cache, timeout)
        body, final_url = await _download(http, target, user_agent, web)   # 5–7
        full = _extract_text(body, final_url)                        # 8. 抽取
        title = _extract_title(body, final_url)                      # 9. 标题
        return _paginate(full, final_url=final_url, title=title,     # 10–11. 分页
                         start_index=start_index,
                         max_length=(web.fetch_max_chars if max_length is None
                                     else max_length))
    finally:
        if own_client:
            await http.aclose()


def _validate_target(url: str) -> str:
    """只放行 http(s) 且必须带主机名；其余（file:// / ftp:// / 空 host）判 `scheme`。"""
    target = (url or "").strip()
    parsed = urlparse(target)
    if parsed.scheme.lower() not in SAFE_URL_SCHEMES or not parsed.hostname:
        raise WebFetchError("scheme", "只支持 http(s) 且必须带主机名")
    return target


async def _reject_private_host(request: httpx.Request) -> None:
    """请求钩子：解析目标主机，命中内网地址就拒绝。

    必须是 `async def`（同步 hook 在 AsyncClient 下 TypeError），DNS 必须走
    `asyncio.to_thread`（同步 getaddrinfo 会阻塞事件循环）。**不改写 request.url**：
    本期只防「直连内网」，不防 DNS rebinding。
    """
    host = request.url.host
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0,
                                        socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebFetchError("dns", f"域名解析失败：{host}") from exc
    except OSError as exc:
        raise WebFetchError("dns", f"域名解析出错：{host}") from exc
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]     # IPv6 带 scope id 时去掉
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise WebFetchError("blocked_host", f"目标是内网地址：{host}")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """六类非公网地址：private / loopback / link_local / multicast / reserved / unspecified。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(bool(getattr(ip, flag)) for flag in _BLOCKED_IP_FLAGS)


async def _check_robots(http: httpx.AsyncClient, url: str, user_agent: str,
                        cache: dict[str, Any] | None, timeout: float) -> None:
    """遵守 robots.txt（只在自主调用时生效）：不可达按拒绝处理，4xx 按放行处理。"""
    parsed = urlparse(url)
    host_key = parsed.netloc.lower()
    parser: RobotFileParser | None = None
    if cache is not None:
        cached = cache.get(host_key)
        if isinstance(cached, RobotFileParser):
            parser = cached
    if parser is None:
        parser = await _load_robots(http, f"{parsed.scheme}://{parsed.netloc}",
                                    user_agent, timeout)
        if cache is not None:
            cache[host_key] = parser
    if not parser.can_fetch(user_agent, url):
        raise WebFetchError("robots", "robots.txt 不允许抓取该地址")


async def _load_robots(http: httpx.AsyncClient, base: str, user_agent: str,
                       timeout: float) -> RobotFileParser:
    """拉一次 robots.txt 并解析。

    5xx / 连接失败 → `robots` 拒绝（拿不到规则时不赌它允许）；4xx → 放行。
    """
    robots_url = f"{base}{ROBOTS_PATH}"
    headers = {"User-Agent": user_agent, "Accept-Encoding": ACCEPT_ENCODING}
    try:
        response = await http.get(robots_url, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise WebFetchError("robots", "robots.txt 请求超时") from exc
    except httpx.HTTPError as exc:
        raise WebFetchError("robots",
                            f"robots.txt 不可达（{type(exc).__name__}）") from exc
    if response.status_code >= 500:
        raise WebFetchError("robots", f"robots.txt 不可达（HTTP {response.status_code}）")
    parser = RobotFileParser()
    if 400 <= response.status_code < 500:
        parser.parse(["User-agent: *", "Allow: /"])   # 没有 robots.txt：按惯例放行
    else:
        parser.parse(response.text.splitlines())
    return parser


async def _download(http: httpx.AsyncClient, url: str, user_agent: str,
                    web: WebConfig) -> tuple[bytes, str]:
    """发一次 GET，返回（正文原始字节已解码 Content-Encoding、最终 URL）。"""
    headers = {"User-Agent": user_agent, "Accept-Encoding": ACCEPT_ENCODING}
    limit = int(web.fetch_max_bytes)
    try:
        async with http.stream("GET", url, headers=headers,
                               timeout=float(web.fetch_timeout_s)) as response:
            _raise_for_status(response)
            declared = _content_length(response)                    # 5. 大小
            if declared is not None and declared > limit:
                raise WebFetchError(
                    "too_large", f"Content-Length {declared} 超过上限 {limit} 字节")
            raw = await _read_raw(response, limit)
            body = _decode_content(raw, _content_encoding(response), limit)  # 6. 编码
            _check_media_type(response)                             # 7. 类型
            return body, str(response.url)
    except httpx.TimeoutException as exc:
        raise WebFetchError("timeout", f"请求超时（{web.fetch_timeout_s}s）") from exc
    except httpx.TooManyRedirects as exc:
        raise WebFetchError("http",
                            f"重定向超过 {web.fetch_max_redirects} 次") from exc
    except httpx.HTTPError as exc:
        raise WebFetchError("http", f"请求失败（{type(exc).__name__}）") from exc


def _raise_for_status(response: httpx.Response) -> None:
    if 200 <= response.status_code < 300:
        return
    raise WebFetchError("http", f"HTTP {response.status_code}")


def _header(response: httpx.Response, name: str) -> str:
    """取响应头并归一成 str（httpx 的 `Headers.get` 注解是 Any，直接返回会被 mypy 拦）。"""
    return str(response.headers.get(name, "")).strip()


def _content_length(response: httpx.Response) -> int | None:
    """Content-Length 只在能解析成数字时用来预判；缺失或异常值交给流式上限兜底。"""
    raw = _header(response, "content-length")
    return int(raw) if raw.isdigit() else None


def _content_encoding(response: httpx.Response) -> str:
    return _header(response, "content-encoding").lower()


async def _read_raw(response: httpx.Response, limit: int) -> bytes:
    """边读边数**原始字节**，超限立即中止（不等整块进内存）。

    必须 `aiter_raw()`：`aiter_bytes()` 会先把 gzip 解开再给，炸弹一次迭代就能吐出
    4194304 字节，那时再判已经晚了。
    """
    buffer = bytearray()
    async for chunk in response.aiter_raw():
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise WebFetchError("too_large", f"响应体超过上限 {limit} 字节")
    return bytes(buffer)


def _decode_content(raw: bytes, encoding: str, limit: int) -> bytes:
    """只认 identity / gzip；其他编码（br / zstd / deflate）判 `content_type`。"""
    if encoding in IDENTITY_ENCODINGS:
        return raw
    if encoding not in GZIP_ENCODINGS:
        raise WebFetchError("content_type", f"不支持的 Content-Encoding：{encoding}")
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        # 解压同样限 fetch_max_bytes：挡的是压缩比炸弹，不是压缩后的字节数
        body = decompressor.decompress(raw, limit)
    except zlib.error as exc:
        raise WebFetchError("content_type", "gzip 解压失败") from exc
    if decompressor.unconsumed_tail:
        raise WebFetchError("too_large", f"解压后超过上限 {limit} 字节")
    if not decompressor.eof:
        raise WebFetchError("content_type", "gzip 流不完整")
    return body


def _check_media_type(response: httpx.Response) -> None:
    media_type = _header(response, "content-type").split(";")[0].strip().lower()
    if media_type not in HTML_MEDIA_TYPES:
        raise WebFetchError("content_type",
                            f"不支持的内容类型：{media_type or '（响应未声明 content-type）'}")


def _extract_text(body: bytes, url: str) -> str:
    """抽正文；`None` / 全空白都判 `insufficient_text`（JS 空壳页就是这条路）。"""
    text = trafilatura.extract(
        body,                     # bytes 直接给：GBK/GB18030 由库自己探测
        url=url,
        output_format="markdown",
        include_comments=False,
        include_tables=False,
        favor_precision=True,     # 只有 True 时 JS 空壳页才落 None
        with_metadata=False,      # True 会让 date 字段凭空捏造
    )
    if text is None or not text.strip():
        raise WebFetchError("insufficient_text", "没有抽出正文（可能是 JS 渲染页）")
    return text


def _extract_title(body: bytes, url: str) -> str:
    """标题：先 `extract_metadata`（取不到时 title 是 None，不是空串），再正则，最后空串。"""
    try:
        # bytes 直接给（与 extract 一致，GBK 标题要靠自动探测）；
        # 2.3.1 的 URL 参数叫 default_url——没有 url= 这个关键字。
        metadata = trafilatura.extract_metadata(body, default_url=url)
    except Exception:            # 元数据抽取失败不该让整次抓取失败
        metadata = None
    if metadata is not None and metadata.title is not None:
        title = metadata.title.strip()
        if title:
            return title
    match = _TITLE_RE.search(body)
    if match is None:
        return ""
    return match.group(1).decode("utf-8", "replace").strip()


def _paginate(full: str, *, final_url: str, title: str, start_index: int,
              max_length: int) -> FetchedPage:
    """把整篇正文切成本次要交给模型的一段，并算出下一页游标。

    「<200 字符判失败」是本项目的**策略阈值**（trafilatura 没有最小长度门槛，30 字照返）；
    判定的是整篇字数，不是本次切片长度——越界翻页不该被判失败。
    """
    char_count = len(full)
    if char_count < MIN_TEXT_CHARS:
        raise WebFetchError(
            "insufficient_text",
            f"正文仅 {char_count} 字符，少于 {MIN_TEXT_CHARS} 字符阈值")
    start = min(max(0, int(start_index)), char_count)
    end = start + max(0, int(max_length))
    text = full[start:end]
    next_start = start + len(text)
    next_start_index = next_start if next_start < char_count else None
    return FetchedPage(
        final_url=final_url,
        title=title,
        text=text,
        char_count=char_count,
        truncated=next_start_index is not None,
        next_start_index=next_start_index,
    )
