"""素材库接口（openapi tag：素材）：浏览、主题筛选、单条详情与整理（S6.1 起）。

- `GET /api/materials`：分页 + 关键词 / 类型 / 打标来源 / 主题目录筛选，附主题目录计数；
- `GET /api/materials/{material_id}`：单条详情（含爆点要素），非法或未知 id 一律 404；
- `PATCH /api/materials/{material_id}`（S6.2）：改标题 / 标签 / 描述 —— 写库 + 写 `.txt` 旁车。
- `POST /api/materials/{material_id}/trash`（S6.3）：连同旁车移进 `<素材根>/_trash/` 并删行；
- `GET /api/materials/trash`：列回收站；`POST /api/materials/trash/restore`：移回原相对路径
  并触发索引（恢复 = 重新入库，新 uuid）；`POST /api/materials/trash/purge`：真删
  （`paths` 或 `all=true` 二选一 + `confirm`）。

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
    list_trash,
    load_material,
    purge_trash,
    restore_from_trash,
    trash_material,
    update_material,
)
from ..deps import ConfigDep, DispatcherDep, SessionDep, request_id_header
from ..models import (
    MaterialDetail,
    MaterialList,
    MaterialSource,
    MaterialTaskAccepted,
    MaterialTrashResult,
    MaterialType,
    TrashList,
    TrashPurgeRequest,
    TrashPurgeResult,
    TrashRestoreRequest,
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


@router.get("/materials/trash", response_model=TrashList,
            responses={503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def list_trash_endpoint(cfg: ConfigDep) -> dict[str, Any]:
    """列回收站里的媒体文件（S6.3）；旁车跟着媒体走，不单独列出。

    这条静态路径必须声明在 `/materials/{material_id}` **之前**——否则 `trash` 会被当成
    一个 material_id（backlog 主题一设计约定 1）。
    """
    return list_trash(cfg)


@router.post("/materials/trash/restore", status_code=202, response_model=MaterialTaskAccepted,
             responses={400: error_response("请求格式或参数不合法"),
                        404: error_response("资源不存在"),
                        409: error_response("目标位置已有同名文件"),
                        422: error_response("字段级校验失败"),
                        503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def restore_material_endpoint(payload: TrashRestoreRequest, cfg: ConfigDep,
                                    dispatcher: DispatcherDep) -> dict[str, Any]:
    """把回收站里的一条移回素材目录并触发索引（恢复 = 重新入库，新 uuid）。"""
    restore_from_trash(cfg, payload.path)
    task_id = await dispatcher.enqueue_index()
    return {"task_id": task_id}


@router.post("/materials/trash/purge", response_model=TrashPurgeResult,
             responses={400: error_response("请求格式或参数不合法"),
                        404: error_response("资源不存在"),
                        422: error_response("字段级校验失败"),
                        503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def purge_trash_endpoint(payload: TrashPurgeRequest, cfg: ConfigDep) -> dict[str, Any]:
    """真删：`paths` 或 `all=true` 二选一，且 `confirm` 必须为 true（二次确认）。"""
    if not payload.confirm:
        raise BadRequestError("真删需要二次确认：confirm 必须为 true")
    if payload.all and payload.paths:
        raise BadRequestError("paths 与 all 只能给一个",
                              {"paths": len(payload.paths or [])})
    if not payload.all and not payload.paths:
        raise BadRequestError("必须给 paths，或 all=true 清空回收站")
    return purge_trash(cfg, paths=payload.paths, all_files=payload.all)


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


@router.post("/materials/{material_id}/trash", response_model=MaterialTrashResult,
             responses={400: error_response("请求格式或参数不合法"),
                        404: error_response("资源不存在"),
                        503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def trash_material_endpoint(material_id: UuidPath, cfg: ConfigDep,
                                  session: SessionDep) -> dict[str, Any]:
    """把素材（连同旁车）移进回收站并删掉索引行（S6.3）。

    删行会级联清掉 `run_matches`——历史候选不再回来（恢复 = 重新入库、新 uuid）。
    """
    result = await trash_material(session, cfg, _material_uuid(material_id))
    if result is None:
        raise NotFoundError(f"素材不存在：{material_id}", {"material_id": material_id})
    return result
