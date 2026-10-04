"""陈旧 run 对账：worker 启动时把卡住的 `running` 行标成 `failed`（S4.1）。

用法：

    # worker 启动时自动跑一次（`tasks/worker.py` 装好信号）
    uv run celery -A xhs_agent.tasks.worker:app worker --loglevel=info

    # 手动跑一次（运维 / 排查）
    uv run python -m xhs_agent.tasks.reconcile

为什么挂在 worker 启动：`acks_late=False`（默认）下 worker 进程崩溃会吞掉手上那个任务，
`runs` 就永远停在 `running`，轮询接口一直返回 409；重启的 worker 在 `worker_ready` 里对账一次，
把超过 `[queue].task_time_limit_s × 2`（默认 900 × 2 = 1800 秒）还没收尾的行标成 `failed`。
这覆盖"worker 崩溃吞任务"与"硬超时 SIGKILL"两条窄场景，不引入 Celery beat（第二个常驻进程与
容器编排归 S4.4）——`run_reconcile` 就是将来 beat 任务体。

口径：对账失败**只记日志、不阻断 worker 启动**（数据库暂时不可用时，真正干活的路径照样会失败
并暴露问题）；成功时逐行 WARNING 一次（带 `run_id`），没有陈旧行时 INFO 一条计数。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from typing import Any

from ..config import AppConfig, load_config
from ..core.logging import bind_run_id, configure_logging, reset_run_id
from ..db.session import create_engine_from_config, create_session_factory
from ..services.runs import reconcile_stale_runs

logger = logging.getLogger("xhs_agent.tasks.reconcile")

# 陈旧阈值 = 硬超时上限的倍数：超过它还没收尾，说明进程已经不在了
STALE_MULTIPLIER = 2


def stale_threshold_s(cfg: AppConfig) -> int:
    """陈旧阈值（秒）：`[queue].task_time_limit_s × 2`，默认 900 × 2 = 1800。"""
    return int(cfg.queue.task_time_limit_s) * STALE_MULTIPLIER


async def _reconcile(cfg: AppConfig, *, now: datetime | None = None) -> list[dict[str, Any]]:
    engine = create_engine_from_config(cfg)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            return await reconcile_stale_runs(session, threshold_s=stale_threshold_s(cfg),
                                              now=now)
    finally:
        await engine.dispose()


def run_reconcile(cfg: AppConfig, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """同步入口（自建 engine、用完 dispose）：返回被标记的行；失败只记日志。

    与 `tasks/analysis.py` 同一口径（ADR 0004）：任务入口是同步函数，内部 `asyncio.run`，
    连接池在本次调用内建、内关，不挂进程级。
    """
    try:
        rows = asyncio.run(_reconcile(cfg, now=now))
    except Exception:  # noqa: BLE001 - 对账是维护动作，失败不能拖垮 worker 启动
        logger.exception("陈旧 run 对账失败（不阻断 worker 启动）")
        return []
    threshold = stale_threshold_s(cfg)
    if not rows:
        logger.info("陈旧运行对账：没有超过 %s 秒仍未收尾的 running 行", threshold)
        return []
    for row in rows:
        token = bind_run_id(str(row.get("run_id") or ""))
        try:
            logger.warning("陈旧运行对账：run %s（job %s，自 %s 起超过 %s 秒未收尾）→ failed",
                           row.get("run_id"), row.get("job_id"), row.get("started_at"), threshold)
        finally:
            reset_run_id(token)
    return rows


def install_worker_ready_reconcile(cfg: AppConfig) -> Any:
    """把对账挂到 worker 的 `worker_ready` 信号（worker 启动时跑一次）；返回处理器便于测试。

    两个细节都是踩过的坑（`app` 参数已去掉：Celery 发的 sender 不是它，过滤反而坏事）：
    - `weak=False`：处理器是闭包，弱引用会被立刻回收，信号就永远不触发；
    - **不加 sender 过滤**：Celery 在 `celery/apps/worker.py` 里是
      `signals.worker_ready.send(sender=consumer)`——sender 是 Consumer 实例而不是 app，
      按 app 过滤会让处理器永不触发（S4.1 真冒烟抓到）。
    """
    from celery.signals import worker_ready

    def _handler(sender: Any = None, **kwargs: Any) -> None:
        run_reconcile(cfg)

    worker_ready.connect(_handler, weak=False)
    return _handler


def main(argv: list[str] | None = None) -> int:
    """手动入口：`uv run python -m xhs_agent.tasks.reconcile`（配置/依赖问题同样不降级）。"""
    parser = argparse.ArgumentParser(prog="python -m xhs_agent.tasks.reconcile",
                                     description="把卡住的 running 运行标成 failed（S4.1 陈旧对账）")
    parser.parse_args(argv)
    cfg = load_config()
    configure_logging(cfg.log_level, cfg.log_format)
    rows = run_reconcile(cfg)
    print(f"陈旧 run 对账完成：标记 {len(rows)} 行 failed（阈值 {stale_threshold_s(cfg)} 秒）",
          file=sys.stderr)
    return 0


if __name__ == "__main__":   # pragma: no cover - 手工入口
    raise SystemExit(main())
