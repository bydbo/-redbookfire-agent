"""声明式基类与约束命名约定。

用途：给所有 ORM 模型一个共同基点，并统一约束/索引命名——命名稳定的元数据让
      Alembic autogenerate（S2.3）只在真实变更时才产生 diff，不会被"自动改名字"淹没。
输入：无。
输出：`Base`（`Base.metadata` 是建表与迁移的唯一依据）。
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# `ck` 规则要求每个 CheckConstraint 都显式给 name（本层所有 CHECK 都已命名）
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
