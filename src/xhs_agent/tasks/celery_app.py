"""Celery 应用工厂：broker 与结果后端都取 `REDIS_URL`，软/硬超时取 `[queue]`（S3.4b / S6.3）。

用途：给 worker 与 API 投递侧造同一份 Celery 配置；**纯工厂**，导入期不读配置（S3.2 口径），
      所以可以离线单测。worker 的模块级 app 在 `tasks/worker.py` 里。
输入：`AppConfig`（读 `env_view["REDIS_URL"]` 与 `[queue]` 的软/硬超时）。
输出：配置好的 `celery.Celery` 实例。
边界：缺 `REDIS_URL` 或前缀不对直接抛 `ConfigError`（配置问题，不降级）。

为什么 S6.3 起**设** result backend（ADR 0014）：素材索引任务（扫描 / 上传 / 回收站恢复）没有
数据库行可以承载进度与失败原因（本轮零迁移、不加表），`GET /api/materials/tasks/{task_id}`
读的就是结果后端。护栏有两层，都是为了不改坏 S3.4b 定下的错误语义：

1. **投递端**：分析任务显式 `ignore_result=True`（`api/deps.py`）——`send_task` 在
   `ignore_result=True` 时*不会*调 `backend.on_task_call`，所以 broker 不可达仍然是快速
   `kombu.exceptions.OperationalError` → 503，而不是 6.8 秒后抛裸
   `RuntimeError("Retry limit exceeded … result store backend")`；
2. **worker 端**：分析任务在 `register_analyze_task` 里也声明 `ignore_result=True`——消息头
   优先于任务声明（`celery.app.trace.get_actual_ignore_result`），两边一致就不会往 Redis 里
   堆没人读的分析结果（ADR 0011：运行状态的权威源是数据库）。
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import setup_logging

from ..config import AppConfig, ConfigError
from ..core.logging import configure_logging

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
    """按配置造 Celery 应用（broker 与结果后端都是 Redis；理由见模块 docstring）。"""
    url = redis_url(cfg)
    app = Celery("xhs_agent", broker=url, backend=url)
    app.conf.update(
        broker_transport_options={"socket_connect_timeout": SOCKET_TIMEOUT_S,
                                  "socket_timeout": SOCKET_TIMEOUT_S,
                                  "retry_policy": dict(RETRY_POLICY)},
        broker_connection_retry_on_startup=True,
        task_soft_time_limit=cfg.queue.task_soft_time_limit_s,
        task_time_limit=cfg.queue.task_time_limit_s,
        # 结果只服务于"这次索引任务跑到哪了"，任务结束后一小时即可回收
        result_expires=3600,
        timezone="Asia/Shanghai",
        enable_utc=True,
    )
    return app


def install_logging(cfg: AppConfig) -> Any:
    """接管 Celery worker 的日志装配（S4.1）；返回接收者，便于调用方/测试解绑。

    为什么必须挂在 `setup_logging` 信号上：Celery 只在**没有任何接收者**时才配置自己的 dictConfig
    （`celery/app/log.py` 里的 `if not receivers:`）——连上这个信号等于声明"日志由我们配"。
    否则 worker 启动时会把我们的 handler 换掉，`XHS_LOG_FORMAT=json` 在 worker 里就静默失效。
    """
    @setup_logging.connect(weak=False)   # type: ignore[untyped-decorator]
    def _configure(**kwargs: Any) -> None:
        configure_logging(cfg.log_level, cfg.log_format)

    return _configure
