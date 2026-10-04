"""分析接口（openapi tag：分析）：提交任务与轮询状态。

- `POST /api/analyze`：按契约校验（1–10 条热点、每条 1–500 字、`topk` 1–20）→
  落库（复用/新建 hotspots、建 runs(queued)、建 run_hotspots 骨架）→ 投递 → 202。
  投递器由 `get_dispatcher` 注入：S3.4b 起默认投 Celery；broker 不可达时显式失败（503），
  不做进程内假执行（进程内后台任务会在重启时静默丢任务）。
- `GET /api/jobs/{job_id}`：返回契约的 `JobStatus`；不存在 → 404。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ...core.errors import NotFoundError
from ...core.logging import bind_run_id
from ...services.runs import load_job, submit_analysis
from ..deps import DispatcherDep, SessionDep
from ..errors import request_id_of

router = APIRouter(tags=["分析"])


class AnalyzeRequest(BaseModel):
    """`POST /api/analyze` 的请求体（对齐 `AnalyzeRequest`，未知字段一律拒绝）。"""

    model_config = ConfigDict(extra="forbid")

    hotspots: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                                    max_length=500)]] = Field(
        min_length=1, max_length=10)
    topk: int = Field(default=5, ge=1, le=20)


@router.post("/analyze", status_code=202)
async def create_analysis(payload: AnalyzeRequest, request: Request,
                          session: SessionDep, dispatcher: DispatcherDep) -> dict[str, str]:
    """提交一个或多个热点：落库后立即返回 `job_id` 与 `run_id`。

    S4.1：提交成功后把 `run_id` 绑进本次请求的日志上下文（该请求后续每条日志都带它），
    并记在 `request.state.run_id` 上——`RequestIdMiddleware` 的访问日志从那里取（BaseHTTPMiddleware
    下 ContextVar 不会回传到父任务，只有 `request.state` 跨得过这个边界）。
    """
    submission = await submit_analysis(session, payload.hotspots, topk=payload.topk,
                                       request_id=request_id_of(request),
                                       enqueue=dispatcher.enqueue)
    request.state.run_id = submission.run_id
    bind_run_id(submission.run_id)
    return {"job_id": submission.job_id, "run_id": submission.run_id}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, session: SessionDep) -> dict[str, Any]:
    """轮询任务状态：succeeded 后凭 `run_id` 取结果，failed 时 `error` 给出原因。"""
    payload = await load_job(session, job_id)
    if payload is None:
        raise NotFoundError(f"job 不存在：{job_id}", {"job_id": job_id})
    return payload
