"""FastAPI 应用工厂与模块级入口。

用途：建应用——挂中间件、注册全局错误处理器、把路由树挂到 `/api` 前缀下；
      模块级的 `app` 供 `uv run uvicorn xhs_agent.api.main:app` 使用。
输入：可选的 `AppConfig`（`create_app(cfg=...)`，仅用于需要提前绑定配置的调用方）。
输出：`FastAPI` 实例；`API_PREFIX` 是路由树与文档页共用的前缀。

口径：
- 导入期**不加载配置**（配置由 `api.deps.get_config` 按需加载，启动前置检查归 S3.8）；
- 前端静态资源（S3.7）：`create_app(cfg=...)` 时立即尝试挂载；否则在 lifespan 启动钩子里
  按 `app.state.config or get_config()` 决定挂不挂。挂载点记在 `app.state.frontend_mounted`，
  重复不会挂两次；S3.8 会在同一个 lifespan 里加启动前置检查；
- 文档页与 openapi.json 也挂在 `/api` 下（ADR 0009：非 `/api` 路径全部交给前端单页应用，
  这样 S3.7 挂载前端时不需要为它们开例外）；
- 五个业务接口与 `/api/health` 的依赖探测属 S3.3，本文件只保证路由树与错误语义。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..config import AppConfig
from .deps import get_config
from .errors import register_exception_handlers
from .frontend import mount_frontend
from .middleware import RequestIdMiddleware
from .routers import analysis, ops, result

API_PREFIX = "/api"
TITLE = "小红书热点搭子 · 热点相关性 API"
DESCRIPTION = "输入热点，在本地素材库中检索可蹭素材并产出文案初稿（契约见 docs/contracts/openapi.yaml）。"


def create_app(cfg: AppConfig | None = None) -> FastAPI:
    """建应用：中间件 + 错误处理器 + `/api` 路由树（+ 按配置挂前端静态资源）。"""
    app = FastAPI(
        title=TITLE,
        description=DESCRIPTION,
        version="0.1.0",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json",
        lifespan=_lifespan,
    )
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    for router in (ops.router, analysis.router, result.router):
        app.include_router(router, prefix=API_PREFIX)
    app.state.frontend_mounted = False
    if cfg is not None:
        app.state.config = cfg
        # 传了配置就在建应用时挂好（测试与需要提前绑定配置的调用方走这条路径）
        mount_frontend(app, cfg, api_prefix=API_PREFIX)
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动钩子：按配置挂载前端静态资源（S3.7）。

    配置来自 `app.state.config`（`create_app(cfg=...)` 注入）或 `deps.get_config()`；后者是
    `uvicorn xhs_agent.api.main:app` 这条路径——导入期不读配置，启动时才读。
    S3.8 会在这个钩子里加 7 步启动前置检查（含 dist 检查），并负责失败时的退出码。
    """
    if not getattr(app.state, "frontend_mounted", False):
        cfg = getattr(app.state, "config", None) or get_config()
        mount_frontend(app, cfg, api_prefix=API_PREFIX)
    yield


app = create_app()
