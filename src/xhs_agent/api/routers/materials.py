"""素材库接口（openapi tag：素材）：浏览、主题筛选、单条详情与整理（S6.1 起）。

- `GET /api/materials`：分页 + 关键词 / 类型 / 打标来源 / 主题目录筛选，附主题目录计数；
- `GET /api/materials/{material_id}`：单条详情（含爆点要素），非法或未知 id 一律 404；
- `PATCH /api/materials/{material_id}`（S6.2）：改标题 / 标签 / 描述 —— 写库 + 写 `.txt` 旁车。

**路由声明顺序**（backlog 主题一设计约定 1）：静态路径（`/materials/trash*`、`/materials/scan`、
`/materials/uploads`、`/materials/tasks/{task_id}`）必须写在本文件的 `/materials/{material_id}`
**之前**——FastAPI 按声明顺序匹配，静态路径写在后面会被动态路由吃掉。

路径参数在契约里是 `format: uuid`，但契约的响应集合里没有 422——非法 uuid 与未知 id
一样按 **404 资源不存在** 处理（与 `result.py` 的 `_run_uuid` 同一口径）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from ...core.errors import BadRequestError, NotFoundError
from ...services.materials import (
    MATERIALS_PAGE_DEFAULT,
    MATERIALS_PAGE_MAX,
    list_materials,
    load_material,
    update_material,
)
from ..deps import ConfigDep, SessionDep, request_id_header
from ..models import (
    MaterialDetail,
    MaterialList,
    MaterialSource,
    MaterialType,
    UpdateMaterialRequest,
    UuidPath,
    error_response,
)

router = APIRouter(tags=["素材"], dependencies=[Depends(request_id_header)])


def _material_uuid(material_id: str) -> uuid.UUID:
    """解析 material_id；非法 uuid 视作"资源不存在"（契约的响应集合里没有 422）。"""
    try:
        return uuid.UUID(material_id)
    except ValueError as exc:
        raise NotFoundError(f"素材不存在：{material_id}",
                            {"material_id": material_id}) from exc


@router.get("/materials", response_model=MaterialList,
            responses={503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def list_materials_endpoint(
        cfg: ConfigDep, session: SessionDep,
        q: Annotated[str | None, Query()] = None,
        type: Annotated[MaterialType | None, Query()] = None,
        source: Annotated[MaterialSource | None, Query()] = None,
        dir: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=MATERIALS_PAGE_MAX)] = MATERIALS_PAGE_DEFAULT,
        offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, Any]:
    """列出素材库：`indexed_at` 倒序分页；`dirs` 只受 q / type / source 影响（S6.1）。"""
    return await list_materials(session, cfg, q=q, type=type, source=source, dir=dir,
                               limit=limit, offset=offset)


@router.get("/materials/{material_id}", response_model=MaterialDetail,
            responses={404: error_response("资源不存在"),
                       503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def get_material(material_id: UuidPath, cfg: ConfigDep,
                       session: SessionDep) -> dict[str, Any]:
    """单个素材的详情（含爆点要素）；非法 uuid 与未知 id 都是 404，不区分原因。"""
    detail = await load_material(session, cfg, _material_uuid(material_id))
    if detail is None:
        raise NotFoundError(f"素材不存在：{material_id}", {"material_id": material_id})
    return detail


@router.patch("/materials/{material_id}", response_model=MaterialDetail,
              responses={400: error_response("请求格式或参数不合法"),
                         404: error_response("资源不存在"),
                         422: error_response("字段级校验失败"),
                         503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def update_material_endpoint(material_id: UuidPath, payload: UpdateMaterialRequest,
                                   cfg: ConfigDep, session: SessionDep) -> dict[str, Any]:
    """改标题 / 标签 / 描述（S6.2）：写库 + 写 `<素材>.txt` 旁车，返回更新后的详情。

    白名单与类型由 `UpdateMaterialRequest`（extra="forbid"）挡住（422）；"一个字段都没给" 是
    业务规则、按 400 返回；标签的清洗与上限（400）在 `services.materials.update_material` 里，
    因为它要先于查库执行（请求体不合法时不该因为 id 是否存在而改变错误码）。
    """
    if payload.title is None and payload.tags is None and payload.description is None:
        raise BadRequestError("至少要改一个字段：title / tags / description")

    detail = await update_material(session, cfg, _material_uuid(material_id),
                                   title=payload.title, tags=payload.tags,
                                   description=payload.description)
    if detail is None:
        raise NotFoundError(f"素材不存在：{material_id}", {"material_id": material_id})
    return detail
