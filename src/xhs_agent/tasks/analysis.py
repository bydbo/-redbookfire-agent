"""分析任务：消费一个 job，跑「索引新鲜度 → 五节点分析 → 逐热点写回」（S3.4b）。

用途：把 `POST /api/analyze` 投进来的 `job_id` 变成一次真实分析并把结果落库。
输入：`job_id` + `AppConfig`；`caller` / `embedder` / `vision` 可注入（离线测试用）。
输出：终态字符串（`succeeded` / `failed`）。

口径（本模块是 ADR 0004 与 ADR 0011 的落点）：
- **统一 async 写法**（ADR 0004 要求一次性定下）：任务入口是同步函数，内部 `asyncio.run(...)`；
  engine 与 httpx 客户端都在任务内建、任务内关，**不把连接池挂进程级**——Celery 每个任务一个
  新事件循环，进程级连接池会绑在已关闭的 loop 上。
- **索引新鲜度前置**：先进增量同步（`sync_materials`），再给新/变更行回填向量
  （`backfill_embeddings`），最后才进 LangGraph。两步失败即整任务失败，不带着缺向量的库分析。
- **重复投递幂等**：`runs.status` 已是终态就直接返回，不重跑、不重复付费。
- **失败路径**：分析类失败由 S3.4a 自己写 `failed`；本模块只兜底"任务整体崩掉"的情况，
  用**独立会话**写 `runs.status='failed'`（原会话可能已回滚，不能复用）。
- **重试**：只有基础设施类错误（DB / 网络 / 超时）走 Celery 重试；分析类失败不重试
  （模型调用已在客户端内重试过，再整批重跑只是重复烧钱）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import InterfaceError, OperationalError

from ..config import AppConfig
from ..db.session import create_engine_from_config, create_session_factory
from ..services.materials import backfill_embeddings, sync_materials
from ..services.runs import load_job, mark_run_failed, planned_hotspots
from ..workflows import run_analysis

ANALYZE_TASK_NAME = "xhs_agent.analyze_run"

# 终态：已经是这些状态就说明这个 job 跑完了（重复投递直接跳过）
TERMINAL_STATUSES = ("succeeded", "failed")

# 基础设施类错误：重试有意义。分析类错误（LLMError / EmbeddingError / SchemaError /
# ConfigError）不在其中——它们重试也拿不到不同结果，只会重复烧钱。
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    OperationalError, InterfaceError, ConnectionError, TimeoutError, OSError)

MAX_RETRY_COUNTDOWN_S = 30

logger = logging.getLogger("xhs_agent.tasks")


async def run_job(job_id: str, cfg: AppConfig, *, caller: Any = None, embedder: Any = None,
                  vision: Any = None, sync_index: bool = True) -> str:
    """任务体（异步）：查状态 → 索引新鲜度 → 分析，返回终态字符串。

    `sync_index=False` 只给离线测试用（跳过素材同步与向量回填）。
    异常原样上抛——重试与失败兜底由 `execute` 决定（那是任务层的事，不是本函数的事）。
    """
    engine = create_engine_from_config(cfg)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            job = await load_job(session, job_id)
            if job is None:
                raise ValueError(f"job 不存在（脏投递或 run 已被删）：{job_id}")
            if job["status"] in TERMINAL_STATUSES:
                logger.info("job %s 已是终态 %s，跳过（重复投递幂等）", job_id, job["status"])
                return str(job["status"])

            if sync_index:
                # 索引新鲜度：素材目录有变化就先增量入库，再把新/变更行的向量补齐——
                # 否则向量通道会静默缺数据（宁可整任务失败，也不带病的库去分析）
                sync_report = await sync_materials(session, cfg, vision=vision)
                embed_report = await backfill_embeddings(session, cfg, embedder=embedder)
                logger.info("索引新鲜度：%s", sync_report.summary())
                logger.info("向量回填：%s", embed_report.summary())

            raws = [raw for _position, raw in await planned_hotspots(session, job["run_id"])]
            result = await run_analysis(raws, cfg=cfg, session=session,
                                        run_id=job["run_id"], caller=caller,
                                        embedder=embedder)
            return result.status
    finally:
        await engine.dispose()


async def _write_failed(cfg: AppConfig, job_id: str, detail: str) -> None:
    """用独立引擎/会话把 run 标成 failed（不复用可能已损坏的原会话）。"""
    engine = create_engine_from_config(cfg)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            job = await load_job(session, job_id)
            if job is None:   # 脏投递：没有行可标，直接算了
                logger.warning("job %s 不存在，无法标记 failed", job_id)
                return
            await mark_run_failed(session, job["run_id"], detail)
    finally:
        await engine.dispose()


def mark_failed(job_id: str, cfg: AppConfig, detail: str) -> None:
    """兜底标记失败；这一步再失败只记日志，不掩盖原始异常。"""
    try:
        asyncio.run(_write_failed(cfg, job_id, detail))
    except Exception:   # noqa: BLE001 - 兜底路径不能再抛，否则原始异常会被吞掉
        logger.exception("把 job %s 标记为 failed 时又失败（原始错误：%s）", job_id, detail)


def execute(task: Any, job_id: str, cfg: AppConfig, *, caller: Any = None,
            embedder: Any = None, vision: Any = None) -> str:
    """Celery 任务的同步外壳：`asyncio.run` + 重试/失败语义（`task` 是绑定的 Task）。"""
    try:
        return asyncio.run(run_job(job_id, cfg, caller=caller, embedder=embedder,
                                   vision=vision))
    except SoftTimeLimitExceeded as exc:
        detail = f"软超时（{cfg.queue.task_soft_time_limit_s}s 上限）：{exc}"
        logger.error("job %s %s，标记 failed（不重试）", job_id, detail)
        mark_failed(job_id, cfg, detail)
        raise
    except TRANSIENT_ERRORS as exc:
        detail = f"{type(exc).__name__}: {exc}"
        retries = int(getattr(task.request, "retries", 0) or 0)
        budget = int(getattr(task, "max_retries", 0) or 0)
        if retries < budget:
            logger.warning("job %s 遇到基础设施类错误，第 %s/%s 次重试：%s",
                           job_id, retries + 1, budget, detail)
            raise task.retry(exc=exc,
                             countdown=min(MAX_RETRY_COUNTDOWN_S, 2 ** (retries + 1))) from exc
        logger.error("job %s 重试 %s 次仍失败，标记 failed：%s", job_id, budget, detail)
        mark_failed(job_id, cfg, detail)
        raise
    except Exception as exc:
        # 分析类失败（或任何非基础设施错误）：不重试，直接标 failed 再上抛
        mark_failed(job_id, cfg, f"{type(exc).__name__}: {exc}")
        raise


def register_analyze_task(app: Any, cfg: AppConfig, *, caller: Any = None,
                          embedder: Any = None, vision: Any = None) -> Any:
    """把分析任务注册到给定 app 上（worker 与集成用例共用同一份注册逻辑）。

    `caller` / `embedder` / `vision` 只在离线测试里注入；生产由 worker 传 `vision=make_describer(cfg)`，
    模型与向量客户端由 `run_analysis` 按配置现造（一次运行一个 httpx 客户端）。
    """
    # Celery 没有 py.typed / 官方 stubs，装饰器在严格模式下只能是 Any（见 pyproject 的
    # mypy override）；任务体本身仍是全类型标注的。
    @app.task(bind=True, name=ANALYZE_TASK_NAME, max_retries=cfg.queue.max_retries)  # type: ignore[untyped-decorator]
    def analyze_run(self: Any, job_id: str) -> str:
        """消费一个 job：索引新鲜度 → 五节点分析 → 逐热点写回。"""
        return execute(self, job_id, cfg, caller=caller, embedder=embedder, vision=vision)

    return analyze_run
