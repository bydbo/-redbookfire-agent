"""前端静态资源挂载（S3.7）：把 `frontend/dist` 挂到根路径，并做 SPA history 回落。

用途：生产与联调时由 FastAPI 同源提供单页应用（ADR 0008 / ADR 0009）——除 `/api` 之外的
      所有路径都交给前端路由；未命中的**无扩展名**路径回落 `index.html`，这样浏览器直接
      刷新 `/runs/abc` 这类前端路由不会 404。
输入：`FastAPI` 应用 + `AppConfig`（读 `[frontend].serve` 与 `[frontend].dist_dir`）。
输出：`mount_frontend()` 返回是否真的挂上了；`require_dist_dir()` 供启动前置检查复用。
边界：`serve = true` 但 dist 缺失/为空时**不挂载、也不静默找一个替代**；启动中止由 S3.8 的
      preflight 调用 `require_dist_dir()` 完成（配置契约 §四 第 7 步）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from ..config import AppConfig, ConfigError

DEFAULT_API_PREFIX = "/api"

FIX_HINT = ("先在 frontend/ 下执行 pnpm install && pnpm build 产出 dist，"
            "或把 config/config.toml 的 [frontend].serve 设为 false 只跑 API")


class SPAStaticFiles(StaticFiles):
    """静态文件 + SPA 回落，`/api` 前缀一律不动。

    - `/api/**`：直接 404，交给全局错误处理器返回契约的 `not_found`——SPA 不能吞掉 API 的 404；
    - 未命中的**无扩展名**路径：回落 `index.html`（前端 history 路由）；
    - 未命中的带扩展名路径（如 `/assets/app.js`）：保持 404——宁可让缺失的静态资源老老实实
      404，也不要把它们变成 200 的 HTML，否则排查起来极其费劲。
    """

    def __init__(self, *, directory: str, api_prefix: str = DEFAULT_API_PREFIX) -> None:
        super().__init__(directory=directory, html=True)
        self.api_prefix = api_prefix.rstrip("/") or DEFAULT_API_PREFIX

    async def get_response(self, path: str, scope: Scope) -> Response:
        # Mount 只改 root_path，scope["path"] 仍是挂载前的完整路径，所以能直接判前缀
        if str(scope.get("path", "")).startswith(self.api_prefix):
            raise HTTPException(status_code=404)
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or Path(path).suffix:
                raise
            return await super().get_response("index.html", scope)


def dist_dir_if_available(cfg: AppConfig) -> str | None:
    """`serve = true` 且 dist 目录存在且非空时返回绝对路径，否则返回 `None`。"""
    if not cfg.frontend.serve:
        return None
    target = Path(cfg.frontend_dist_dir())
    if not target.is_dir():
        return None
    if not any(target.iterdir()):
        return None
    return str(target)


def mount_frontend(app: FastAPI, cfg: AppConfig, *,
                   api_prefix: str = DEFAULT_API_PREFIX) -> bool:
    """按配置把 SPA 挂到根路径；返回"当前是否已挂载"。重复调用不会挂两次。"""
    if getattr(app.state, "frontend_mounted", False):
        return True
    dist = dist_dir_if_available(cfg)
    if dist is None:
        return False
    # 必须在 /api 路由注册之后调用：Mount("/") 会兜住所有没被前面路由匹配的路径
    app.mount("/", SPAStaticFiles(directory=dist, api_prefix=api_prefix), name="spa")
    app.state.frontend_mounted = True
    return True


def require_dist_dir(cfg: AppConfig) -> str | None:
    """启动前置检查第 7 步（S3.8 调用）：`serve = false` → `None`；可用 → 绝对路径；否则抛错。"""
    if not cfg.frontend.serve:
        return None
    dist = dist_dir_if_available(cfg)
    if dist is None:
        raise ConfigError(
            f"[frontend].serve = true 但前端构建产物不可用：{cfg.frontend_dist_dir()}",
            FIX_HINT)
    return dist
