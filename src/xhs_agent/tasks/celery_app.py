"""Celery 应用工厂：broker 取 `REDIS_URL`，软/硬超时取 `[queue]`（S3.4b）。

用途：给 worker 与 API 投递侧造同一份 Celery 配置；**纯工厂**，导入期不读配置（S3.2 口径），
      所以可以离线单测。worker 的模块级 app 在 `tasks/worker.py` 里。
输入：`AppConfig`（读 `env_view["REDIS_URL"]` 与 `[queue]` 的软/硬超时）。
输出：配置好的 `celery.Celery` 实例。
边界：缺 `REDIS_URL` 或前缀不对直接抛 `ConfigError`（配置问题，不降级）。

为什么**不设 result backend**：运行状态的权威源是数据库（ADR 0011），我们从不读 Celery 结果。
设了后端反而更糟——`send_task` 会顺带碰后端，broker 不可达时实测要 6.8 秒并抛一个裸
`RuntimeError("Retry limit exceeded … result store backend")`；不设后端则直接抛
`kombu.exceptions.OperationalError("Timeout connecting to server")`，语义清楚、能精确映射 503。
"""

from __future__ import annotations

from celery import Celery

from ..config import AppConfig, ConfigError

# 连接与重试都调紧：broker 不可达时要尽快失败（API 拿 503，而不是等默认的几十秒重试）
SOCKET_TIMEOUT_S = 1
RETRY_POLICY = {"max_retries": 1, "interval_start": 0,
                "interval_step": 0.2, "interval_max": 0.5}


def redis_url(cfg: AppConfig) -> str:
    """取并校验 `REDIS_URL`（broker 用）；缺失或前缀不对抛 `ConfigError`。"""
    url = (cfg.env_view.get("REDIS_URL") or "").strip()
    if not url:
        raise ConfigError(
            "缺少 REDIS_URL：任务队列（Celery）必须先配置",
            "把 REDIS_URL=redis://localhost:6379/0 写进 config/.env"
            "（本地依赖用 docker compose up -d --wait 起 Redis），或导出同名环境变量")
    if not url.startswith(("redis://", "rediss://")):
        raise ConfigError(f"REDIS_URL 必须以 redis:// 或 rediss:// 开头：{url}",
                          "按 docs/contracts/配置契约.md §2.1 修正连接串前缀")
    return url


def build_celery_app(cfg: AppConfig) -> Celery:
    """按配置造 Celery 应用（broker=Redis；不设 result backend，见模块 docstring）。"""
    app = Celery("xhs_agent", broker=redis_url(cfg))
    app.conf.update(
        broker_transport_options={"socket_connect_timeout": SOCKET_TIMEOUT_S,
                                  "socket_timeout": SOCKET_TIMEOUT_S,
                                  "retry_policy": dict(RETRY_POLICY)},
        broker_connection_retry_on_startup=True,
        task_soft_time_limit=cfg.queue.task_soft_time_limit_s,
        task_time_limit=cfg.queue.task_time_limit_s,
        timezone="Asia/Shanghai",
        enable_utc=True,
    )
    return app
