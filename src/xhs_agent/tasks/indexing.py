"""素材索引任务（S6.3）：增量同步 + 向量回填，供扫描 / 上传 / 回收站恢复共用。

用途：把「素材目录变了」变成一次真实的入库与回填。三个入口（S6.4 的扫描与上传、S6.3 的
      回收站恢复）投的都是这一个任务，避免三套索引路径各写一遍。
输入：`AppConfig`；`vision`（视觉打标可调用对象，None = 不打标）、`max_vision_items`
      （本次打标上限）、`embedder`（离线测试注入）可注入。
输出：计数 dict（同步 + 回填 + 一句人类可读摘要），任务过程中用 `update_state` 上报阶段。

口径（与 `tasks/analysis.py` 同一套，见 ADR 0004）：

- 任务入口是**同步函数**，内部 `asyncio.run(...)`；engine 在任务内建、任务内关，
  不把连接池挂进程级（Celery 每个任务一个新事件循环）。
- **不重试**：增量同步按 `mtime+size` 判断变化、天然幂等，失败就直接记 FAILURE，用户点
  「扫描」或下次分析前的索引新鲜度都会补上；失败原因写进结果后端的 meta（前端读得到）。
- 视觉打标是付费能力：`vision=None`（缺 ffmpeg 或没开 `[vision]`）时只做旁车 / 文件名打标。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..config import AppConfig
from ..db.session import create_engine_from_config, create_session_factory
from ..services.materials import backfill_embeddings, sync_materials

INDEX_TASK_NAME = "xhs_agent.index_materials"

# 一次索引最多给多少条素材做视觉打标（与 `scripts/index_materials.py` 的默认一致）
DEFAULT_MAX_VISION_ITEMS = 50

# 阶段名（`GET /api/materials/tasks/{task_id}` 的 `step` 直接透出这两句）
STEP_SYNC = "扫描素材目录"
STEP_EMBED = "向量回填"

# Celery 的标准任务状态；读不到结果（键过期 / 从没投过）时是 PENDING
TASK_STATES = ("PENDING", "STARTED", "RETRY", "SUCCESS", "FAILURE")


def task_view(task_id: str, state: str, info: Any) -> dict[str, Any]:
    """Celery 的 state / meta → 契约 `MaterialTask` 形状（S6.4）。

    - `SUCCESS`：`meta` 就是任务返回的计数 dict，`summary` 取其中的一句话摘要；
    - `FAILURE`：`info` 是异常对象，截断成 500 字的 `summary`（**不含密钥**，任务本身不打印密钥）；
    - 其余（含 `STARTED`）：`meta` 里带的是阶段名；
    - 无法识别的状态归 `STARTED`（宁可显示"进行中"，也不抛 500 让前端瞎猜）。
    """
    normalized = state if state in TASK_STATES else "STARTED"
    step: str | None = None
    summary: str | None = None
    result: dict[str, Any] | None = None
    if normalized == "SUCCESS" and isinstance(info, dict):
        summary = str(info.get("summary") or "")[:500] or None
        result = {str(key): value for key, value in info.items()}
    elif normalized == "FAILURE":
        summary = str(info)[:500] if info is not None else None
    elif isinstance(info, dict):
        step = str(info.get("step") or "") or None
    return {"task_id": task_id, "state": normalized, "step": step,
            "summary": summary, "result": result}


async def run_index(cfg: AppConfig, *, vision: Any = None, embedder: Any = None,
                    max_vision_items: int = DEFAULT_MAX_VISION_ITEMS,
                    on_step: Callable[[str], None] | None = None) -> dict[str, Any]:
    """任务体（异步）：增量同步 → 向量回填，返回计数 dict。"""
    engine = create_engine_from_config(cfg)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            if on_step is not None:
                on_step(STEP_SYNC)
            sync = await sync_materials(session, cfg, vision=vision,
                                        max_vision_items=max_vision_items)
            if on_step is not None:
                on_step(STEP_EMBED)
            embed = await backfill_embeddings(session, cfg, embedder=embedder)
        return {
            "scanned": sync.scanned,
            "added": sync.added,
            "updated": sync.updated,
            "unchanged": sync.unchanged,
            "deleted": sync.deleted,
            "vision_used": sync.vision_used,
            "embedded": embed.embedded,
            "summary": f"{sync.summary()}；{embed.summary()}",
        }
    finally:
        await engine.dispose()


def register_index_task(app: Any, cfg: AppConfig, *, vision: Any = None, embedder: Any = None,
                        max_vision_items: int = DEFAULT_MAX_VISION_ITEMS) -> Any:
    """把素材索引任务注册到给定 app 上（worker 与集成用例共用同一份注册逻辑）。

    `force_paths` 目前**不用**：增量同步本身就会覆盖所有变化（上传的新文件天然是"新增"），
    参数留着是为了将来做"只重建这几条"的精确重打标，那时再实现它。
    """
    # Celery 没有 py.typed / 官方 stubs，装饰器在严格模式下只能是 Any（见 pyproject 的
    # mypy override）；任务体本身仍是全类型标注的。
    @app.task(bind=True, name=INDEX_TASK_NAME)   # type: ignore[untyped-decorator]
    def index_materials(self: Any, force_paths: list[str] | None = None) -> dict[str, Any]:
        """跑一次增量索引；阶段进度写进结果后端供 API 查询。"""
        def step(name: str) -> None:
            self.update_state(state="STARTED", meta={"step": name})

        return asyncio.run(run_index(cfg, vision=vision, embedder=embedder,
                                     max_vision_items=max_vision_items, on_step=step))

    return index_materials
