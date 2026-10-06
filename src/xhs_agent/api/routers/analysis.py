"""分析接口（openapi tag：分析）：提交任务、轮询状态与图片热点解析。

- `POST /api/analyze`：按契约校验（1–10 条热点、每条 1–500 字、`topk` 1–20）→
  落库（复用/新建 hotspots、建 runs(queued)、建 run_hotspots 骨架）→ 投递 → 202。
  投递器由 `get_dispatcher` 注入：S3.4b 起默认投 Celery；broker 不可达时显式失败（503），
  不做进程内假执行（进程内后台任务会在重启时静默丢任务）。
- `GET /api/jobs/{job_id}`：返回契约的 `JobStatus`；不存在 → 404。
- `POST /api/hotspots/image-clue`（S6.6）：一张图 → 热点描述 + 爆点线索；图片只在内存里转成模型
  输入，不落盘不入库；`[vision]` 未启用或缺密钥 → 503，格式/体积不合法 → 400，上游失败 → 502。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Request, UploadFile

from ...core.errors import BadRequestError, NotFoundError
from ...core.logging import bind_run_id
from ...services.image_clue import parse_image_clue
from ...services.runs import load_job, submit_analysis
from ...tools.vision import sniff_image_mime
from ..deps import ConfigDep, DispatcherDep, SessionDep, request_id_header
from ..errors import request_id_of
from ..models import (
    AnalyzeAccepted,
    AnalyzeRequest,
    ImageClueResult,
    JobStatus,
    error_response,
)

router = APIRouter(tags=["分析"], dependencies=[Depends(request_id_header)])

REQUEST_ID_HEADER = {"X-Request-ID": {"description": "本次请求的标识",
                                      "schema": {"type": "string"}}}

# 单张图片上限（契约 § /api/hotspots/image-clue）：边读边计，超限立刻 400
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024


async def _read_upload(file: UploadFile) -> bytes:
    """读上传图片并做体积上限：超限立刻 400（不把整张大图读进内存再判断）。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise BadRequestError(
                f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB 上限",
                {"limit_bytes": MAX_IMAGE_BYTES})
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise BadRequestError("图片为空：file 字段没有内容")
    return data


@router.post("/hotspots/image-clue", response_model=ImageClueResult,
             responses={400: error_response("请求格式或参数不合法"),
                        422: error_response("字段级校验失败"),
                        502: error_response("上游服务错误"),
                        503: error_response("依赖不可用（不做降级，直接返回错误）")})
# `json_schema_extra` 让 FastAPI 导出契约要的 `format: binary`
# （新版本默认只写 contentMediaType，openapi-typescript 与工具链都认 format: binary）
async def create_image_clue(
        cfg: ConfigDep,
        file: Annotated[UploadFile, File(json_schema_extra={"format": "binary"})],
) -> ImageClueResult:
    """上传一张图片，拆成热点描述与爆点要素（结果不落库，由用户确认后再提交）。"""
    data = await _read_upload(file)
    mime = sniff_image_mime(data)
    if not mime:
        raise BadRequestError("只支持 jpg / png / webp 图片（按文件内容判断）",
                              {"content_type": file.content_type or ""})
    parsed = await parse_image_clue(cfg, data, mime)
    return ImageClueResult(raw_text=parsed.raw_text, clue=parsed.clue.to_dict(),
                           prompt_versions=parsed.prompt_versions)


@router.post("/analyze", status_code=202, response_model=AnalyzeAccepted,
             responses={
                 202: {"model": AnalyzeAccepted, "description": "任务已接受",
                       "headers": REQUEST_ID_HEADER},
                 400: error_response("请求格式或参数不合法"),
                 422: error_response("字段级校验失败"),
                 502: error_response("上游服务错误"),
                 503: error_response("依赖不可用（不做降级，直接返回错误）"),
             })
async def create_analysis(payload: AnalyzeRequest, request: Request,
                          session: SessionDep, dispatcher: DispatcherDep) -> dict[str, str]:
    """提交一个或多个热点：落库后立即返回 `job_id` 与 `run_id`。

    S4.1：提交成功后把 `run_id` 绑进本次请求的日志上下文（该请求后续每条日志都带它），
    并记在 `request.state.run_id` 上——`RequestIdMiddleware` 的访问日志从那里取（BaseHTTPMiddleware
    下 ContextVar 不会回传到父任务，只有 `request.state` 跨得过这个边界）。
    """
    submission = await submit_analysis(session, payload.hotspots, topk=payload.topk,
                                       request_id=request_id_of(request),
                                       clues=_clue_dicts(payload),
                                       enqueue=dispatcher.enqueue)
    request.state.run_id = submission.run_id
    bind_run_id(submission.run_id)
    return {"job_id": submission.job_id, "run_id": submission.run_id}


def _clue_dicts(payload: AnalyzeRequest) -> list[dict[str, Any] | None] | None:
    """把请求里的 `clues` 折成 JSON 可入库的 dict 列表；长度不匹配按 400 处理（S6.7）。"""
    if payload.clues is None:
        return None
    if len(payload.clues) != len(payload.hotspots):
        raise BadRequestError(
            "clues 必须与 hotspots 一一对应（等长）",
            {"hotspots": len(payload.hotspots), "clues": len(payload.clues)})
    # exclude_unset：只落用户实际确认过的字段，不塞 pydantic 的默认空值（domain 层会补默认）
    return [None if clue is None else clue.model_dump(exclude_unset=True)
            for clue in payload.clues]


@router.get("/jobs/{job_id}", response_model=JobStatus,
            responses={404: error_response("资源不存在"),
                       503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def get_job(job_id: str, session: SessionDep) -> dict[str, Any]:
    """轮询任务状态：succeeded 后凭 `run_id` 取结果，failed 时 `error` 给出原因。"""
    payload = await load_job(session, job_id)
    if payload is None:
        raise NotFoundError(f"job 不存在：{job_id}", {"job_id": job_id})
    return payload
