"""请求中间件：分配 / 透传 `X-Request-ID`，回写响应头，并记一条访问日志。

用途：一次请求的开头确定 request_id（客户端给了就用客户端的），写进 `request.state` 与日志上下文，
      结束前回写到响应头；同时把访问信息（method / path / status / latency）记进日志。
输入：ASGI 应用；请求头 `X-Request-ID`（可选）。
输出：响应头 `X-Request-ID`（一定存在且与请求头一致）；`request.state.request_id`。

边界：未捕获异常在这里被兜住——记完整堆栈到日志，返回契约形状的 500（响应头仍是同一个
request_id）。这样 500 也满足「响应回写 X-Request-ID」，不会绕过中间件。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from ..core.errors import InternalError
from ..core.logging import request_id_var
from .errors import error_response, request_id_of

HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """给每个请求一个 id，并保证它在响应头与日志里都对得上。"""

    def __init__(self, app: ASGIApp, header: str = HEADER) -> None:
        super().__init__(app)
        self.header = header
        self.logger = logging.getLogger("xhs_agent.api.access")

    async def dispatch(self, request: Request,
                       call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        incoming = (request.headers.get(self.header) or "").strip()
        request_id = incoming or str(uuid.uuid4())
        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:  # 兜住未捕获异常：堆栈进日志，响应仍是契约形状
                self.logger.exception("未捕获异常：%s %s", request.method, request.url.path)
                response = error_response(InternalError(), request_id_of(request))
            latency_ms = int((time.perf_counter() - started) * 1000)
            response.headers[self.header] = request_id
            self.logger.info("request %s %s -> %s（%s ms）", request.method, request.url.path,
                             response.status_code, latency_ms)
            return response
        finally:
            request_id_var.reset(token)
