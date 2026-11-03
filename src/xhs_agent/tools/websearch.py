"""SearXNG 搜索适配器：一次 `GET /search?format=json` → 归一化的命中列表。

用途：给对话 agent 的搜索工具提供唯一的搜索出口；**只做「HTTP 请求 + 归一化」**，
      不认识工具上下文，不 import `services/` / `workflows/` / `db/` / `api/`（AGENTS.md §2.0）。
输入：`query` / `count` + `AppConfig`（读 `[web]` 段的 search_url、engines、
      max_results、snippet_chars、search_timeout_s、api_key）；`client` 可注入（单测用
      `httpx.MockTransport`），传 None 时按 search_timeout_s 临时建一个并在返回前关闭。
输出：`SearchResult`（`hits` + `unresponsive`，后者是 SearXNG `unresponsive_engines` 的原样透出，
      供上层写提示文案）。
侧效应：发一次 HTTP 请求；不落盘、不写日志、不改全局状态。
错误语义：失败一律抛 `WebSearchError(reason)`，由上层 handler 映射成 ToolOutcome：
      `config`（search_url 缺失/非法、403 —— 实例没开 json 输出）、`timeout`、
      `http`（其它非 200 与连接类失败）、`schema`（200 但不是 JSON / 结构不符）。
      **本模块不重试**：对话里的搜索失败要让人看见，静默重试只会拖长一轮。

协议口径按真实实例实测，与直觉不同的三点：
1. 顶层只有 `query / results / answers / corrections / infoboxes / suggestions / unresponsive_engines`
   七键——**没有** `number_of_results`，也没有任何分页 / 总数字段，不要试图读它们；
2. 摘要字段是 `content`（不是 `snippet`），日期字段是 `publishedDate`（camelCase，
   无日期时**键存在、值为 `null`）；
3. `format=json` 没开时实测是 **403 + HTML**，非法 format 名会 **200 + HTML 静默降级**——
   所以必须先判状态码、再校 content-type，最后才 `response.json()`。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from ..config import AppConfig
from .http import build_http_client

# 实测的固定请求参数：语言与安全搜索按产品口径写死，一次只取第一页（pageno 恒为 1）
FIXED_PARAMS: dict[str, str] = {
    "format": "json",
    "language": "zh-CN",
    "safesearch": "1",
    "pageno": "1",
}

HTTP_OK = 200
HTTP_FORBIDDEN = 403

# 进结果的 URL 只允许 http(s)：`javascript:` / `data:` 会被模型当成链接回吐给用户
SAFE_URL_SCHEMES = ("http", "https")

# JSON 响应可接受的 content-type（实例与反向代理的实测写法）
JSON_MEDIA_TYPE = "application/json"


@dataclass(frozen=True)
class SearchHit:
    """一条搜索结果。`published_date` 原样保留 SearXNG 的字符串，无日期时是 None。"""

    title: str
    url: str
    snippet: str
    published_date: str | None = None


@dataclass(frozen=True)
class SearchResult:
    """一次搜索的归一化结果。

    `unresponsive` 是 SearXNG `unresponsive_engines` 的原样（形如 `[["google", "timeout"]]`）：
    0 条命中时唯一的健康信号就在这里，不改写成别的结构。
    """

    hits: list[SearchHit]
    unresponsive: list[list[str]]


class WebSearchError(Exception):
    """搜索失败。`reason` ∈ `config / timeout / http / schema`，上层据此决定提示文案。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


async def search_web(
    client: httpx.AsyncClient | None,
    cfg: AppConfig,
    query: str,
    *,
    count: int,
) -> SearchResult:
    """按 `[web]` 配置搜一次，返回至多 `min(count, max_results)` 条命中。

    `client` 由调用方持有（本函数不关闭）；传 None 时临时自建并关闭。
    复用注入的 client 时**显式传 `timeout=`**：共享 client 的超时是 120s，
    搜索必须受 `search_timeout_s` 约束。
    """
    web = cfg.web
    endpoint = _endpoint(web.search_url)
    params = {**FIXED_PARAMS, "q": query}
    engines = web.engines.strip()
    if engines:                      # 空 = 不传，用实例自己的默认引擎
        params["engines"] = engines
    headers: dict[str, str] = {}
    api_key = web.api_key
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = float(web.search_timeout_s)
    own_client = client is None
    http = client or build_http_client(timeout)
    try:
        try:
            response = await http.get(endpoint, params=params, headers=headers,
                                      timeout=timeout)
        except httpx.TimeoutException as exc:
            raise WebSearchError("timeout", f"搜索超时（{timeout:g}s）") from exc
        except httpx.InvalidURL as exc:   # 端口写成 notaport 之类：地址本身配错了
            raise WebSearchError("config", "search_url 不是合法的 http(s) 地址") from exc
        except httpx.HTTPError as exc:    # 连不上 / DNS 失败等
            raise WebSearchError("http", f"请求失败（{type(exc).__name__}）") from exc
        _raise_for_status(response)
        return _normalize(response, limit=min(count, web.max_results),
                          snippet_chars=web.snippet_chars)
    finally:
        if own_client:
            await http.aclose()


def _endpoint(search_url: str) -> str:
    """`search_url` → `/search` 端点（去掉末尾斜杠，保留路径前缀）。

    文案里**不带地址本身**（可能内嵌凭据，§八 密钥不进日志）。
    """
    base = (search_url or "").strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme.lower() not in SAFE_URL_SCHEMES or not parsed.netloc:
        raise WebSearchError("config", "search_url 缺失或不是 http(s) 地址")
    return f"{base}/search"


def _raise_for_status(response: httpx.Response) -> None:
    """先判状态码再看 body：json 没开时实测是 403 + HTML，直接 `.json()` 会给出误导性错误。"""
    if response.status_code == HTTP_OK:
        return
    if response.status_code == HTTP_FORBIDDEN:
        raise WebSearchError(
            "config", "实例拒绝搜索请求（HTTP 403），检查 search.formats 是否包含 json")
    raise WebSearchError("http", f"HTTP {response.status_code}")


def _normalize(response: httpx.Response, *, limit: int,
               snippet_chars: int) -> SearchResult:
    """JSON → `SearchResult`：保序过滤非 http(s) 与重复 URL，按 `limit` 截断。"""
    body = _json_body(response)
    raw_results = body.get("results")
    if not isinstance(raw_results, list):
        raise WebSearchError("schema", "响应缺少 results 数组")
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in raw_results:
        if len(hits) >= limit:
            break
        if not isinstance(item, dict):
            raise WebSearchError("schema", "results 里出现了非对象元素")
        url = str(item.get("url") or "").strip()
        if urlparse(url).scheme.lower() not in SAFE_URL_SCHEMES or url in seen:
            continue
        seen.add(url)
        hits.append(SearchHit(
            title=str(item.get("title") or ""),
            url=url,
            snippet=str(item.get("content") or "")[:snippet_chars],
            published_date=_optional_text(item.get("publishedDate")),
        ))
    return SearchResult(hits=hits,
                        unresponsive=_unresponsive(body.get("unresponsive_engines")))


def _json_body(response: httpx.Response) -> dict[str, object]:
    """校验 content-type 后再解析：非法 format 名会 200 + HTML 静默降级。"""
    media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type and media_type != JSON_MEDIA_TYPE and not media_type.endswith("+json"):
        raise WebSearchError("schema", f"响应不是 JSON（content-type: {media_type}）")
    try:
        body = response.json()
    except ValueError as exc:
        raise WebSearchError("schema", "响应不是合法 JSON") from exc
    if not isinstance(body, dict):
        raise WebSearchError("schema", "响应顶层不是 JSON 对象")
    return body


def _optional_text(value: object) -> str | None:
    """`publishedDate` 无日期时是 null（键存在）：转 None，不写空串。"""
    text = str(value or "").strip()
    return text or None


def _unresponsive(raw: object) -> list[list[str]]:
    """`unresponsive_engines` 原样透出；结构异常的元素丢掉，不为它整次失败。"""
    if not isinstance(raw, list):
        return []
    return [[str(part) for part in entry] for entry in raw if isinstance(entry, list)]
