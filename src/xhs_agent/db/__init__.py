"""数据库层：SQLAlchemy 模型、会话工厂与引擎。

用途：把 `docs/contracts/数据契约.md` 的五张表映射成 ORM 模型，并提供从 `AppConfig`
构造的异步引擎 / 会话工厂（缺 `DATABASE_URL` 时直接报错，不做降级）。
输入：`AppConfig`（读 `[database]` 与 `DATABASE_URL`）。
输出：`Base`、五个模型、`database_url()`、`create_engine_from_config()`、
`create_session_factory()`。
"""

from .base import Base
from .models import Hotspot, Material, Run, RunHotspot, RunMatch
from .session import create_engine_from_config, create_session_factory, database_url

__all__ = [
    "Base",
    "Hotspot",
    "Material",
    "Run",
    "RunHotspot",
    "RunMatch",
    "create_engine_from_config",
    "create_session_factory",
    "database_url",
]
