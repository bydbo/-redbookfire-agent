"""FastAPI 应用工厂与模块级入口。

用途：建应用——挂中间件、注册全局错误处理器、把路由树挂到 `/api` 前缀下；
      模块级的 `app` 供 `uv run uvicorn xhs_agent.api.main:app` 使用。
输入：可选的 `AppConfig`（`create_app(cfg=...)`，仅用于需要提前绑定配置的调用方）。
输出：`FastAPI` 实例；`API_PREFIX` 是路由树与文档页共用的前缀。

口径：
- 导入期**不加载配置**（配置由 `api.deps.get_config` 按需加载）；
- 启动前置检查（S3.8）：lifespan 先按 `app.state.config or get_config()` 跑《配置契约》§四的
  7 步检查，**不过就不启动**（抛 `PreflightFailed`，问题清单已先打印）；`create_app(cfg, ...)`
  传了配置就按 `check_startup` 决定查不查，测试/工具可显式关掉。精确的退出码 2/3 走
  `python -m xhs_agent.serve`——uvicorn 自己在 lifespan 失败时固定退 3；
- 前端静态资源（S3.7）：`create_app(cfg=...)` 时立即尝试挂载；否则在 lifespan 启动钩子里
  按 `app.state.config or get_config()` 决定挂不挂。挂载点记在 `app.state.frontend_mounted`，
  重复不会挂两次；
- 文档页与 openapi.json 也挂在 `/api` 下（ADR 0009：非 `/api` 路径全部交给前端单页应用，
  这样 S3.7 挂载前端时不需要为它们开例外）；
- 五个业务接口与 `/api/health` 的依赖探测属 S3.3，本文件只保证路由树与错误语义。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.gzip import GZipMiddleware

from ..config import AppConfig
from ..core.logging import configure_logging
from ..core.tracing import flush_tracer, get_tracer, install_fastapi_instrumentation
from ..probe import PreflightFailed, report_lines, run_startup_checks
from .deps import dispose_engine, get_config
from .errors import register_exception_handlers
from .frontend import mount_frontend
from .middleware import RequestIdMiddleware
from .routers import analysis, chat, materials, ops, result

API_PREFIX = "/api"
TITLE = "小红书热点搭子 · 热点相关性 API"
DESCRIPTION = "输入热点，在本地素材库中检索可蹭素材并产出文案初稿（契约见 docs/contracts/openapi.yaml）。"

logger = logging.getLogger("xhs_agent.api")


def create_app(cfg: AppConfig | None = None, *, check_startup: bool = True) -> FastAPI:
    """建应用：中间件 + 错误处理器 + `/api` 路由树（+ 按配置挂前端静态资源）。

    `check_startup`：lifespan 启动时要不要跑 7 步前置检查（默认跑）。测试与"已经自己查过"的
    调用方（`python -m xhs_agent.serve`）传 `False`。
    """
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
    # S5.7：压缩文本类响应（JS / CSS / HTML / JSON）。托管前端产物的是这个应用，
    # 移动端 Lighthouse 的瓶颈就是「1 MB JS 原样传输」；图片等已压缩类型在
    # Starlette 的排除清单里，不会被二次压缩。
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    # S4.3：ASGI 服务端 span（最后加 = 最外层，覆盖整条请求；没有 provider 时是空操作）
    install_fastapi_instrumentation(app)
    register_exception_handlers(app)
    for router in (ops.router, analysis.router, result.router, materials.router, chat.router):
        app.include_router(router, prefix=API_PREFIX)
    app.state.frontend_mounted = False
    app.state.check_startup = check_startup
    if cfg is not None:
        app.state.config = cfg
        # 传了配置就在建应用时挂好（测试与需要提前绑定配置的调用方走这条路径）
        mount_frontend(app, cfg, api_prefix=API_PREFIX)
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动钩子：先做启动前置检查（S3.8），再挂前端静态资源（S3.7）。

    配置来自 `app.state.config`（`create_app(cfg=...)` 注入）或 `deps.get_config()`；后者是
    `uvicorn xhs_agent.api.main:app` 这条路径——导入期不读配置，启动时才读。
    检查不过就打印问题清单并抛 `PreflightFailed`，uvicorn 会中止启动（那条路径恒退 3）。
    """
    cfg = getattr(app.state, "config", None) or get_config()
    # S4.1：日志格式与级别在这里落地——`create_app(cfg=...)` 这条路径不会走 `get_config()`，
    # 少了这一句 `XHS_LOG_FORMAT=json` 就只在"没传配置"的分支才生效（幂等，重复调用无害）
    configure_logging(cfg.log_level, cfg.log_format)
    # S4.2 / S4.3：追踪是旁路组件——缺 LANGFUSE_* 只打一条 warning（在 get_tracer 里）；
    # API 进程也要建一次客户端，否则 ASGI 服务端 span 没有导出通道
    get_tracer(cfg)
    if getattr(app.state, "check_startup", True):
        report = await run_startup_checks(cfg)
        if not report.ok:
            for line in report_lines(report):
                print(line, file=sys.stderr)
            print(f"启动前置检查未通过（阶段 {report.stage}）：{len(report.problems)} 项问题，"
                  f"退出码 {report.exit_code}（uvicorn 这条路径固定退 3）", file=sys.stderr)
            raise PreflightFailed(report)
    if not getattr(app.state, "frontend_mounted", False):
        mount_frontend(app, cfg, api_prefix=API_PREFIX)
    try:
        yield
    finally:
        # S4.3：退出前把缓冲里的 span 刷出去
        flush_tracer()
        # S4.4：释放进程级连接池并清配置缓存（E3 审查建议 6），停机在日志里可见
        await dispose_engine()
        logger.info("API 退出：释放连接池与配置缓存")


app = create_app()
