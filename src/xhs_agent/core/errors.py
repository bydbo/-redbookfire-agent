"""API 错误层级：把「哪里出错」翻译成契约里的错误码与 HTTP 状态。

用途：接口层直接抛这些异常，由 `api/errors.py` 的处理器统一序列化成
      `openapi.yaml` 的 `ErrorResponse`；错误码与 HTTP 状态严格按契约配对。
输入：`message`（给人看的一句话，缺省用该错误码的标准文案）与可选的 `detail`（结构化补充）。
输出：异常对象，带 `code` / `status` / `message` / `detail` 四个可读属性。
"""

from __future__ import annotations

from typing import Any

# 契约错误码（docs/contracts/openapi.yaml 的「错误码」表）
BAD_REQUEST = "bad_request"
VALIDATION_ERROR = "validation_error"
NOT_FOUND = "not_found"
DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
UPSTREAM_ERROR = "upstream_error"
INTERNAL_ERROR = "internal_error"

ERROR_CODES: tuple[str, ...] = (BAD_REQUEST, VALIDATION_ERROR, NOT_FOUND,
                                DEPENDENCY_UNAVAILABLE, UPSTREAM_ERROR, INTERNAL_ERROR)

# 错误码 → HTTP 状态（与契约表一一对应）
CODE_STATUS: dict[str, int] = {
    BAD_REQUEST: 400,
    VALIDATION_ERROR: 422,
    NOT_FOUND: 404,
    DEPENDENCY_UNAVAILABLE: 503,
    UPSTREAM_ERROR: 502,
    INTERNAL_ERROR: 500,
}

# HTTP 状态 → 契约错误码：处理框架自己抛的 HTTPException（如 405 Method Not Allowed）
STATUS_CODE_MAP: dict[int, str] = {
    400: BAD_REQUEST,
    404: NOT_FOUND,
    405: BAD_REQUEST,
    409: BAD_REQUEST,
    422: VALIDATION_ERROR,
    502: UPSTREAM_ERROR,
    503: DEPENDENCY_UNAVAILABLE,
}

DEFAULT_MESSAGE: dict[str, str] = {
    BAD_REQUEST: "请求不合法",
    VALIDATION_ERROR: "字段校验失败",
    NOT_FOUND: "资源不存在",
    DEPENDENCY_UNAVAILABLE: "依赖服务不可用",
    UPSTREAM_ERROR: "上游服务返回错误",
    INTERNAL_ERROR: "服务内部错误",
}


def code_for_status(status: int) -> str:
    """HTTP 状态 → 契约错误码（未列出的按 5xx / 4xx 归类，保证 code 永远是契约枚举之一）。"""
    if status in STATUS_CODE_MAP:
        return STATUS_CODE_MAP[status]
    return INTERNAL_ERROR if status >= 500 else BAD_REQUEST


class ApiError(Exception):
    """所有对外可见错误的基类；子类只固定 `code`。"""

    code: str = INTERNAL_ERROR

    def __init__(self, message: str = "", detail: dict[str, Any] | None = None) -> None:
        self.message = message or DEFAULT_MESSAGE[self.code]
        self.detail: dict[str, Any] = dict(detail or {})
        super().__init__(self.message)

    @property
    def status(self) -> int:
        return CODE_STATUS[self.code]


class BadRequestError(ApiError):
    """请求格式或参数不合法（400）。"""

    code = BAD_REQUEST


class NotFoundError(ApiError):
    """job_id / run_id 等资源不存在（404）。"""

    code = NOT_FOUND


class DependencyUnavailableError(ApiError):
    """数据库、Redis 或模型服务不可用（503）。"""

    code = DEPENDENCY_UNAVAILABLE


class UpstreamError(ApiError):
    """上游模型返回错误或连续重试失败（502）。"""

    code = UPSTREAM_ERROR


class InternalError(ApiError):
    """未分类的内部错误（500）。"""

    code = INTERNAL_ERROR
