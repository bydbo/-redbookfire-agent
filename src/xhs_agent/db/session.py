"""异步引擎与会话工厂：从 `AppConfig` 构造，缺配置直接报错（不降级）。

用途：把"配置 → DSN → 引擎 → 会话工厂"这条链路收敛到一处，供仓储层与服务层复用。
输入：`AppConfig`（`DATABASE_URL` 取进程环境变量 / `config/.env`，池参数取 `[database]` 段）。
输出：`AsyncEngine` 与 `async_sessionmaker[AsyncSession]`；未建立真实连接（惰性）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import AppConfig, ConfigError

DSN_PREFIX = "postgresql+asyncpg://"


def database_url(cfg: AppConfig) -> str:
    """取并校验 `DATABASE_URL`；缺失或前缀不对时抛 `ConfigError`（附修复提示）。"""
    dsn = cfg.env_view.get("DATABASE_URL")
    if not dsn:
        raise ConfigError(
            "缺少 DATABASE_URL：数据库连接串必须先配置",
            "把 DATABASE_URL=postgresql+asyncpg://xhs:xhs@localhost:5432/xhs 写进 config/.env"
            "（本地依赖用 docker compose up -d --wait 起），或导出同名环境变量")
    if not dsn.startswith(DSN_PREFIX):
        raise ConfigError(
            f"DATABASE_URL 必须以 {DSN_PREFIX} 开头（asyncpg 驱动）",
            "按 docs/contracts/配置契约.md §2.1 修正连接串前缀")
    return dsn


def create_engine_from_config(cfg: AppConfig) -> AsyncEngine:
    """按 `[database]` 段的池参数创建异步引擎（构造期不连库）。"""
    settings = cfg.database
    return create_async_engine(
        database_url(cfg),
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        echo=settings.echo,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """创建会话工厂；`expire_on_commit=False` 让提交后的对象仍可读取属性。"""
    return async_sessionmaker(engine, expire_on_commit=False)
