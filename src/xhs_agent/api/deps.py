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

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated, Protocol

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ..config import AppConfig, load_config
from ..core.errors import DependencyUnavailableError
from ..core.logging import configure_logging
from ..db.session import create_engine_from_config, create_session_factory


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """加载并缓存配置（进程级）；顺带把日志级别配好。"""
    cfg = load_config()
    configure_logging(cfg.log_level)
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


class QueueNotConfigured:
    """S3.4b 之前没有任务队列：显式失败，不做进程内假执行。

    为什么不做假执行：进程内后台任务会在重启时静默丢任务，与"DB 是运行状态唯一权威源"
    （ADR 0011）和"不做降级"的口径都冲突。接口集成用例用 `dependency_overrides`
    注入假投递器来验证 202 分支。
    """

    async def enqueue(self, job_id: str) -> None:
        raise DependencyUnavailableError(
            "分析队列未接线（Celery 属 S3.4b，当前未安装）", {"job_id": job_id})


class Dispatcher(Protocol):
    """任务投递器：只要能把 `job_id` 投出去即可（S3.4b 的 Celery 实现也满足它）。"""

    async def enqueue(self, job_id: str) -> None: ...


def get_dispatcher() -> Dispatcher:
    """任务投递器依赖；S3.4b 把它换成 Celery 实现。"""
    return QueueNotConfigured()


# 路由签名里用 Annotated 别名注入依赖（不用 `= Depends(...)` 默认值，避免 ruff B008）
ConfigDep = Annotated[AppConfig, Depends(get_config)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
DispatcherDep = Annotated[Dispatcher, Depends(get_dispatcher)]
