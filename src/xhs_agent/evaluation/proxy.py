"""E9 本地模型转发服务：统一预留、转发、按 usage 结算。

用途：API / worker / 索引 / 视觉 / embedding 只拿到 localhost 临时凭据，真实供应商凭据
只存在于转发进程。每个请求在发送前从 `BudgetLedger` 预留；流式断流或缺 usage 转未知占用。

边界：
- 只允许冻结的模型、路径和方法；不缓存、不伪造响应；
- 响应与 SSE 原样转发，只替换鉴权头并补 `X-E9-Request-Id`；
- 转发异常不会退回“假成功”，调用方收到上游错误或 502/504。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .budget import BudgetError, BudgetLedger, PriceTable

HOP_HEADERS = {
    "host", "content-length", "connection", "authorization", "accept-encoding",
    "transfer-encoding", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "upgrade",
}
ALLOWED_PATHS = {"/chat/completions", "/embeddings"}


@dataclass(frozen=True)
class ProxyConfig:
    """转发白名单与上游；密钥只从环境注入，不写日志。"""

    upstream_base_url: str
    upstream_api_key: str
    local_api_key: str = ""
    timeout_s: float = 120.0

    def upstream_url(self, path: str) -> str:
        if path not in ALLOWED_PATHS:
            raise ValueError(f"路径不在白名单：{path}")
        return f"{self.upstream_base_url.rstrip('/')}{path}"


def _kind_for(path: str, body: dict[str, Any]) -> str:
    if path == "/embeddings":
        return "embedding"
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return "vision"
    return "text"


def _usage_tokens(usage: Any) -> tuple[int | None, int | None]:
    if not isinstance(usage, dict):
        return None, None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (int(prompt) if isinstance(prompt, (int, float)) else None,
            int(completion) if isinstance(completion, (int, float)) else None)


def _actual_cost(prices: PriceTable, model: str, kind: str,
                 prompt: int | None, completion: int | None) -> float | None:
    if prompt is None:
        return None
    price = prices.models.get(model)
    if price is None:
        return None
    output = completion if kind != "embedding" else 0
    if output is None:
        return None
    return round((prompt * price.input_per_m + output * price.output_per_m) / 1_000_000, 8)


def create_app(config: ProxyConfig, ledger: BudgetLedger,
               prices: PriceTable, *, client: httpx.AsyncClient | None = None) -> FastAPI:
    """构造 ASGI app；测试可注入 `httpx.MockTransport` 客户端。"""
    app = FastAPI(title="E9 model proxy", docs_url=None, redoc_url=None)
    owned_client = client is None
    http_client = client or httpx.AsyncClient(timeout=config.timeout_s)

    @app.on_event("shutdown")
    async def _close() -> None:
        if owned_client:
            await http_client.aclose()

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, **ledger.summary()}

    @app.api_route("/v1/{path:path}", methods=["POST"])
    async def forward_v1(path: str, request: Request) -> Response:
        return await _forward(f"/{path}", request)

    @app.post("/chat/completions")
    async def forward_chat(request: Request) -> Response:
        return await _forward("/chat/completions", request)

    @app.post("/embeddings")
    async def forward_embeddings(request: Request) -> Response:
        return await _forward("/embeddings", request)

    async def _forward(path: str, request: Request) -> Response:
        if path not in ALLOWED_PATHS:
            return JSONResponse({"error": "path_not_allowed"}, status_code=404)
        if config.local_api_key:
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {config.local_api_key}":
                return JSONResponse({"error": "local_auth_failed"}, status_code=401)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "json_object_required"}, status_code=400)
        model = str(body.get("model") or "")
        if model not in prices.models:
            return JSONResponse({"error": "model_not_allowed", "model": model},
                                status_code=403)
        kind = _kind_for(path, body)
        request_id = uuid.uuid4().hex
        try:
            ledger.reserve(request_id, model, kind, attempt_id="",
                           upstream=config.upstream_base_url)
        except BudgetError as exc:
            return JSONResponse({"error": "budget_rejected", "detail": str(exc)},
                                status_code=402)
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() not in HOP_HEADERS}
        headers["Authorization"] = f"Bearer {config.upstream_api_key}"
        headers["Accept"] = "text/event-stream" if body.get("stream") else "application/json"
        try:
            if body.get("stream"):
                return await _stream(path, body, headers, request_id, model, kind)
            upstream = await http_client.post(config.upstream_url(path), json=body,
                                               headers=headers, timeout=config.timeout_s)
        except httpx.HTTPError as exc:
            ledger.mark_unknown(request_id, f"upstream transport: {type(exc).__name__}")
            return JSONResponse({"error": "upstream_unavailable",
                                 "detail": type(exc).__name__}, status_code=502)
        try:
            payload = upstream.json()
        except json.JSONDecodeError:
            ledger.mark_unknown(request_id, "upstream non-json")
            return Response(content=upstream.content, status_code=upstream.status_code,
                            headers={"X-E9-Request-Id": request_id})
        usage = payload.get("usage") if isinstance(payload, dict) else None
        prompt, completion = _usage_tokens(usage)
        cost = _actual_cost(prices, model, kind, prompt, completion)
        if cost is None:
            ledger.mark_unknown(request_id, "missing usage")
        else:
            ledger.settle(request_id, cost, usage if isinstance(usage, dict) else {})
        out_headers = {key: value for key, value in upstream.headers.items()
                       if key.lower() not in HOP_HEADERS}
        out_headers["X-E9-Request-Id"] = request_id
        return Response(content=upstream.content, status_code=upstream.status_code,
                        headers=out_headers, media_type=upstream.headers.get("content-type"))

    async def _stream(path: str, body: dict[str, Any], headers: dict[str, str],
                      request_id: str, model: str, kind: str) -> Response:
        upstream = http_client.stream("POST", config.upstream_url(path), json=body,
                                      headers=headers, timeout=config.timeout_s)
        try:
            response = await upstream.__aenter__()
        except httpx.HTTPError as exc:
            ledger.mark_unknown(request_id, f"stream connect: {type(exc).__name__}")
            return JSONResponse({"error": "upstream_unavailable",
                                 "detail": type(exc).__name__}, status_code=502)

        async def chunks() -> AsyncIterator[bytes]:
            usage: dict[str, Any] | None = None
            try:
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        raw = line[5:].strip()
                        if raw and raw != "[DONE]":
                            try:
                                event = json.loads(raw)
                            except json.JSONDecodeError:
                                event = None
                            if isinstance(event, dict) and isinstance(event.get("usage"), dict):
                                usage = event["usage"]
                    yield (line + "\n").encode("utf-8")
            finally:
                # 客户端断流 / 生成器取消也会走到这里：缺 usage 时保留未知占用。
                await upstream.__aexit__(None, None, None)
                prompt, completion = _usage_tokens(usage)
                cost = _actual_cost(prices, model, kind, prompt, completion)
                try:
                    if cost is None:
                        ledger.mark_unknown(request_id, "stream missing usage")
                    else:
                        ledger.settle(request_id, cost, usage or {})
                except BudgetError:
                    # 重复结算不应把已经转发给客户端的流改成 500；账本保持原状态。
                    pass

        out_headers = {key: value for key, value in response.headers.items()
                       if key.lower() not in HOP_HEADERS}
        out_headers["X-E9-Request-Id"] = request_id
        return StreamingResponse(chunks(), status_code=response.status_code,
                                 headers=out_headers,
                                 media_type=response.headers.get("content-type",
                                                                 "text/event-stream"))
    return app
