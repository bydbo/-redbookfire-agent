"""错误响应：把异常统一序列化成契约的 `ErrorResponse`。

用途：注册全局异常处理器，保证任何失败路径都返回 `{"code", "message", "detail"}` 三键；
      中间件也复用它来构造 500（这样响应头同样带上 `X-Request-ID`）。
输入：FastAPI 应用（`register_exception_handlers`）与运行时的异常对象。
输出：`JSONResponse`，HTTP 状态与 `code` 严格按契约配对（见 `core.errors.CODE_STATUS`）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..config import ConfigError
from ..core.errors import (
    CODE_STATUS,
    DEFAULT_MESSAGE,
    DEPENDENCY_UNAVAILABLE,
    INTERNAL_ERROR,
    UPSTREAM_ERROR,
    VALIDATION_ERROR,
    ApiError,
    InternalError,
    code_for_status,
)
from ..core.logging import current_request_id
from ..tools.embedding import EmbeddingError
from ..tools.llm import LLMError

logger = logging.getLogger("xhs_agent.api")


def request_id_of(request: Request) -> str:
    """请求 id：优先取中间件写进 `request.state` 的值，兜底取上下文（便于单元测试直调）。"""
    from_state = getattr(request.state, "request_id", "")
    return str(from_state or current_request_id())


def json_error(code: str, message: str, detail: dict[str, Any]) -> JSONResponse:
    """统一错误响应：三个键恒在（契约里 `detail` 可选，我们总是给，属超集）。"""
    return JSONResponse(status_code=CODE_STATUS[code],
                        content={"code": code, "message": message, "detail": detail})


def error_response(exc: ApiError, request_id: str) -> JSONResponse:
    """由 `ApiError`（或它的子类）构造响应，`detail` 里补上 `request_id`。"""
    detail: dict[str, Any] = {**exc.detail, "request_id": request_id}
    return json_error(exc.code, exc.message, detail)


def register_exception_handlers(app: FastAPI) -> None:
    """注册四类处理器：自定义 API 错误 / 参数校验 / 框架 HTTPException / 未捕获异常。"""

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        if exc.code == INTERNAL_ERROR:
            logger.error("api error: %s", exc.message, exc_info=exc)
        return error_response(exc, request_id_of(request))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request,
                                       exc: RequestValidationError) -> JSONResponse:
        # exc.errors() 里可能带不可序列化的 ctx（如 ValueError 实例），统一过 jsonable_encoder
        detail = {"request_id": request_id_of(request), "errors": jsonable_encoder(exc.errors())}
        return json_error(VALIDATION_ERROR, "字段校验失败", detail)

    @app.exception_handler(ConfigError)
    async def _handle_config_error(request: Request, exc: ConfigError) -> JSONResponse:
        # 缺 DSN、缺密钥这类"依赖没配好"按依赖不可用处理（503），而不是 500
        detail = {"request_id": request_id_of(request), "fix": exc.fix}
        return json_error(DEPENDENCY_UNAVAILABLE, exc.message, detail)

    async def _handle_db_unavailable(request: Request, exc: Exception) -> JSONResponse:
        # 连接级数据库错误：堆栈进日志，对外只给 503
        request_id = request_id_of(request)
        logger.exception("数据库不可用（request_id=%s）", request_id)
        return json_error(DEPENDENCY_UNAVAILABLE, "数据库不可用",
                          {"request_id": request_id,
                           "error": f"{type(exc).__name__}: {exc}"[:200]})

    for exc_type in (OperationalError, InterfaceError):
        app.add_exception_handler(exc_type, _handle_db_unavailable)

    async def _handle_upstream_error(request: Request, exc: Exception) -> JSONResponse:
        # 上游模型 / 向量服务失败（重试后仍失败）：按契约折成 502 upstream_error。
        # 现在没有 API 请求会同步调上游（分析在 worker 里跑），这条映射是给
        # S3.4b/S3.6 的调用链备好的——先按契约落位，不等到报 500 才补。
        request_id = request_id_of(request)
        logger.warning("上游调用失败（request_id=%s）：%s", request_id, exc)
        return json_error(UPSTREAM_ERROR, DEFAULT_MESSAGE[UPSTREAM_ERROR],
                          {"request_id": request_id,
                           "error": f"{type(exc).__name__}: {exc}"[:200]})

    for upstream_type in (LLMError, EmbeddingError):
        app.add_exception_handler(upstream_type, _handle_upstream_error)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request,
                                     exc: StarletteHTTPException) -> JSONResponse:
        # 框架自己抛的（如 404 未知路径、405 方法不允许）也折成契约错误码：
        # message 用本项目的中文标准文案，框架原文与原始状态码留在 detail 里备查。
        code = code_for_status(exc.status_code)
        detail: dict[str, Any] = {"request_id": request_id_of(request),
                                  "original_status": exc.status_code,
                                  "detail": str(exc.detail)}
        return json_error(code, DEFAULT_MESSAGE[code], detail)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 堆栈只进日志；响应体固定文案 + request_id，绝不外泄异常信息
        request_id = request_id_of(request)
        logger.exception("未处理异常（request_id=%s）", request_id)
        return error_response(InternalError(), request_id)
