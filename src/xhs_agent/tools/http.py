"""统一的 httpx 异步客户端与退避策略（S3.5）。

用途：给文本模型客户端与向量客户端提供同一个 `httpx.AsyncClient`（连接池 + 超时），
      并把重试退避算法收在一处，避免两个客户端各写一套。
输入：`timeout_s`（默认超时，秒）与重试次数 `attempt`。
输出：可 `async with` 的 `httpx.AsyncClient`；`backoff_seconds(attempt)` 的退避秒数。

边界：本模块**不自持、不缓存**客户端——调用方用 `async with` 持有并关闭。
为什么不做进程级单例：Celery 每个任务内建一个事件循环，进程级连接池会绑在已关闭的
循环上；run 级客户端既拿到连接复用，又不会跨事件循环。
"""

from __future__ import annotations

import httpx

# 连接池上限：分析链路是低并发（一次运行内模型与向量基本是顺序调用），留出余量即可。
# 上限不进《配置契约》：改这里要同步 docs/技术栈.md。
MAX_CONNECTIONS = 10
MAX_KEEPALIVE_CONNECTIONS = 5
KEEPALIVE_EXPIRY_S = 30.0
DEFAULT_TIMEOUT_S = 60.0

# 退避上限：失败重试最多等 8 秒，避免异常路径把一次运行拖到很久。
MAX_BACKOFF_S = 8.0


def build_http_client(timeout_s: float = DEFAULT_TIMEOUT_S) -> httpx.AsyncClient:
    """建一个带连接池的异步客户端；调用方负责关闭（`async with`）。"""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(float(timeout_s)),
        limits=httpx.Limits(max_connections=MAX_CONNECTIONS,
                            max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
                            keepalive_expiry=KEEPALIVE_EXPIRY_S),
        follow_redirects=False,
    )


def backoff_seconds(attempt: int) -> float:
    """第 `attempt` 次失败后的退避秒数：`min(8.0, 1.5 ** attempt)`（口径与 S2.5 一致）。

    单独成函数是为了让重试策略可被离线单测，不必在用例里给 `asyncio.sleep` 打补丁。
    """
    return min(MAX_BACKOFF_S, 1.5 ** max(1, int(attempt)))
