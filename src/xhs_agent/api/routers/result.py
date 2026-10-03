"""结果接口（openapi tag：结果）：读取运行结果与报告。

- `GET /api/runs/{run_id}`：契约 `RunDetail`；不存在 → 404；尚未完成 → 409。
- `GET /api/runs/{run_id}/report?format=html|md`：从库里即时渲染（ADR 0011：DB 是权威源），
  返回 `text/html` 或 `text/markdown`；不存在 → 404；尚未完成 → 409。

路径参数在契约里是 `format: uuid`，但契约只列出 404/409/503 三种响应——
因此非法 uuid 按 **404 资源不存在** 处理，不引入 422。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import Response

from ...config import PROJECT_ROOT
from ...core.errors import ConflictError, NotFoundError
from ...services.runs import UNFINISHED_STATUSES, load_report_model, load_run
from ...tools.report import render_html, render_markdown
from ..deps import ConfigDep, SessionDep

router = APIRouter(tags=["结果"])


def _run_uuid(run_id: str) -> uuid.UUID:
    """解析 run_id；非法 uuid 视作"资源不存在"（契约的响应集合里没有 422）。"""
    try:
        return uuid.UUID(run_id)
    except ValueError as exc:
        raise NotFoundError(f"run 不存在：{run_id}", {"run_id": run_id}) from exc


def _require_finished(payload: dict[str, Any]) -> None:
    if payload["status"] in UNFINISHED_STATUSES:
        raise ConflictError("运行尚未完成", {"run_id": payload["run_id"],
                                             "status": payload["status"]})


@router.get("/runs/{run_id}")
async def get_run(run_id: str, session: SessionDep) -> dict[str, Any]:
    """读取运行结果：线索、候选素材、覆盖缺口与文稿。"""
    payload = await load_run(session, _run_uuid(run_id))
    if payload is None:
        raise NotFoundError(f"run 不存在：{run_id}", {"run_id": run_id})
    _require_finished(payload)
    return payload


@router.get("/runs/{run_id}/report")
async def get_run_report(run_id: str, session: SessionDep, cfg: ConfigDep,
                         format: Annotated[str, Query(pattern="^(html|md)$")] = "html") -> Response:
    """读取报告：`format=html`（默认）返回单文件 HTML，`format=md` 返回 Markdown 文本。"""
    run_uuid = _run_uuid(run_id)
    payload = await load_run(session, run_uuid)
    if payload is None:
        raise NotFoundError(f"run 不存在：{run_id}", {"run_id": run_id})
    _require_finished(payload)
    model = await load_report_model(session, cfg, run_uuid)
    if model is None:  # pragma: no cover - 与上一段同一行数据，理论上不会发生
        raise NotFoundError(f"run 不存在：{run_id}", {"run_id": run_id})
    if format == "md":
        return Response(content=render_markdown(model), media_type="text/markdown; charset=utf-8")
    return Response(content=render_html(model, base_dir=PROJECT_ROOT),
                    media_type="text/html; charset=utf-8")
