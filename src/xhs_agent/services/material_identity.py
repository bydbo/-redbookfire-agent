"""素材身份查询与索引互斥；当前素材缺失时不按路径重新绑定。

输入 AsyncSession 和 UUID；输出规范 UUID / 当前行。别名必须单跳且无环，异常数据明确失败。
同步锁使用 PostgreSQL 事务锁，在提交 / 回滚时释放，不依赖 Celery 或 Redis 入口。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import ConflictError
from ..db.models import Material, MaterialIdAlias

MATERIAL_INDEX_LOCK = 613001


async def lock_material_index(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                          {"key": MATERIAL_INDEX_LOCK})


async def canonical_id(session: AsyncSession, material_id: uuid.UUID) -> uuid.UUID:
    alias = (await session.execute(select(MaterialIdAlias).where(
        MaterialIdAlias.old_id == material_id))).scalar_one_or_none()
    if alias is None:
        return material_id
    nested = (await session.execute(select(MaterialIdAlias.old_id).where(
        MaterialIdAlias.old_id == alias.canonical_id))).scalar_one_or_none()
    if nested is not None or alias.canonical_id == material_id:
        raise ConflictError("素材身份别名出现链或环，需修复身份数据")
    return alias.canonical_id


async def current_material(session: AsyncSession, material_id: uuid.UUID) -> Material | None:
    resolved = await canonical_id(session, material_id)
    return (await session.execute(select(Material).where(Material.id == resolved))).scalar_one_or_none()
