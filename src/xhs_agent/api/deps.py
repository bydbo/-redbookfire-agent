"""依赖注入：配置、数据库会话与任务投递三个可覆盖的依赖。

用途：接口层用 `Depends(get_config)` 拿配置、`Depends(get_session)` 拿异步会话、
      `Depends(get_dispatcher)` 投递异步任务；三者都是进程级缓存 + 可覆盖的。
输入：无（走 `load_config()` 的默认三层优先级与 `[database]` 池参数）。
输出：`AppConfig` / `AsyncSession` / 投递器；测试用
      `app.dependency_overrides[...]` 覆盖（接口集成用例就是这么注入容器库与假队列的）。

注意：导入本模块或 `api.main` 都**不会**读配置——配置只在首个请求（或显式调用）时加载，
启动前置检查（含 FastAPI lifespan）归 S3.8。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated, Any, Protocol

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ..config import AppConfig, load_config
from ..core.errors import DependencyUnavailableError
from ..core.logging import configure_logging
from ..db.session import create_engine_from_config, create_session_factory


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """加载并缓存配置（进程级）；顺带把日志级别与输出格式配好。"""
    cfg = load_config()
    configure_logging(cfg.log_level, cfg.log_format)
    return cfg


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """按配置建异步引擎（进程级缓存；构造期不建立连接）。缺 DSN 时抛 `ConfigError`，
    由 `api.errors` 折成 503。"""
    return create_engine_from_config(get_config())


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """按引擎建会话工厂（进程级缓存）。"""
    return create_session_factory(get_engine())


async def get_session() -> AsyncIterator[AsyncSession]:
    """请求级异步会话依赖。"""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def dispose_engine() -> None:
    """进程退出时释放进程级资源（S4.4）：dispose 引擎并清掉三个 `lru_cache`。

    为什么必须做：优雅停机（docker compose 的 SIGTERM / uvicorn 收尾）之后进程要退出，
    进程级连接池不 dispose 就会在数据库侧留下一批半开连接；清缓存则保证进程若被复用
    （测试、`uvicorn --reload`）时重新按最新配置建引擎与会话。
    """
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_session_factory.cache_clear()
    get_engine.cache_clear()
    get_config.cache_clear()


async def request_id_header(
    x_request_id: Annotated[str, Header(alias="X-Request-ID")] = "",
) -> None:
    """只为了在 OpenAPI 里声明契约的 `X-Request-ID` 请求头（S4.6）。

    取值仍由 `api/middleware.py` 的中间件处理（它把 id 写进 `request.state.request_id`
    与日志上下文）；这个依赖本身不使用参数，用途只是让 FastAPI 把它导出进 schema。
    默认空串 = 非必填，导出形状与契约 `components.parameters.RequestId` 一致。
    """


class Dispatcher(Protocol):
    """任务投递器：只要能把 `job_id` 投出去即可（S3.4b 的 Celery 实现也满足它）。"""

    async def enqueue(self, job_id: str) -> None: ...


class CeleryDispatcher:
    """把 job 投到 Celery（broker=Redis）的任务投递器。

    为什么在 `enqueue` 里现造 app：`build_celery_app` 要读 `REDIS_URL`，而导入期不许读配置
    （S3.2 的"导入期不读配置"口径）——所以延迟到第一次投递时才导入 `tasks` 包并用本进程缓存的
    `get_config()` 建 app。
    `send_task` 是同步网络调用，放线程里执行以免阻塞事件循环；broker 不可达时折成
    `DependencyUnavailableError`（503），此时 `submit_analysis` 会整体回滚，不留脏 queued 行。
    """

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._app: Any = None

    def app(self) -> Any:
        """进程内缓存的 Celery 应用（首次调用才读配置）。"""
        if self._app is None:
            from ..tasks.celery_app import build_celery_app
            self._app = build_celery_app(self.cfg)
        return self._app

    async def enqueue(self, job_id: str) -> None:
        from kombu.exceptions import KombuError

        from ..core.tracing import current_trace_headers
        from ..tasks.analysis import ANALYZE_TASK_NAME

        # S4.3：把本次请求的 W3C traceparent 放进消息头，worker 接着这条 trace 往下走
        headers = current_trace_headers()
        try:
            await asyncio.to_thread(self.app().send_task, ANALYZE_TASK_NAME, args=[job_id],
                                    headers=headers)
        except (KombuError, OSError) as exc:
            raise DependencyUnavailableError(
                "分析队列不可用：投递失败",
                {"job_id": job_id, "error": f"{type(exc).__name__}: {exc}"[:200]}) from exc


def get_dispatcher(cfg: ConfigDep) -> Dispatcher:
    """任务投递器依赖：S3.4b 起默认投 Celery。

    走 `ConfigDep` 而不是直接调 `get_config()`：这样测试覆盖 `get_config` 时投递器也会用
    测试配置（否则 `dependency_overrides` 对嵌套的直接函数调用不生效）。
    """
    return CeleryDispatcher(cfg)


# 路由签名里用 Annotated 别名注入依赖（不用 `= Depends(...)` 默认值，避免 ruff B008）
ConfigDep = Annotated[AppConfig, Depends(get_config)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
DispatcherDep = Annotated[Dispatcher, Depends(get_dispatcher)]
