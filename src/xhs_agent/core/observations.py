"""上下文与缓存度量事件（S7.28）。

用途：把「状态裁决、预算装配、版本提交、缓存查询、缓存消费、模型尝试」落成**本地事实源**
（`context_observations`），供报告脚本按日期 / 模型 / prompt / 缓存层复算。
输入：`AsyncSession` 或会话工厂、事件类型、业务键、结构化 payload。
输出：是否真正写入（重复投递返回 False，不重复计数）。

口径（方案 §9.4）：

- **稳定 event_id**：`sha256(event_type + session_id + 业务键)` 派生；同一事实重复投递按主键
  冲突忽略，不重复计数；
- **旁路**：写入失败只告警，绝不阻断业务；用 savepoint 隔离，失败不会污染调用方的事务；
- **只放元数据**：payload 里不放用户原文、图片、工具参数正文或素材路径——`sanitize` 会
  丢弃这些键并截断长文本；
- **叶子调用才计数**：模型尝试按每次**实际传输**一行；不写轮次 / 工具汇总，避免重复相加。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, cast

from sqlalchemy import CursorResult
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ContextObservation

logger = logging.getLogger("xhs_agent.observations")

EVENT_TYPES: tuple[str, ...] = (
    "context_state_decision",
    "context_assembled",
    "artifact_version_committed",
    "cache_request_started",
    "cache_lookup_finished",
    "cache_entry_created",
    "cache_entry_invalidated",
    "cache_entry_consumed",
    "artifact_consumed",
    "model_attempt_started",
    "model_attempt_finished",
)
#: payload 中禁止出现的键（用户原文 / 图片 / 工具参数 / 素材路径）
BLOCKED_KEYS = frozenset({
    "user_text", "content", "evidence", "args", "arguments", "prompt", "system",
    "messages", "path", "image", "image_bytes", "raw_text", "text", "reply",
})
#: 单个字符串字段的保留长度
MAX_STRING_CHARS = 120


def event_id(event_type: str, session_id: str, key: str) -> str:
    """稳定事件 ID：同一 (类型, 会话, 业务键) 永远得到同一个值。"""
    body = f"{event_type}|{session_id}|{key}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def sanitize(payload: dict[str, Any] | None) -> dict[str, Any]:
    """递归清洗：丢禁止键、截断长字符串、只保留 JSON 可序列化的标量 / 容器。"""
    def _clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): _clean(item)
                for key, item in value.items()
                if str(key).lower() not in BLOCKED_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [_clean(item) for item in value[:20]]
        if isinstance(value, str):
            return value if len(value) <= MAX_STRING_CHARS else value[:MAX_STRING_CHARS] + "…"
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:MAX_STRING_CHARS]

    cleaned: dict[str, Any] = _clean(dict(payload or {}))
    return cleaned


async def record(
    session: AsyncSession,
    *,
    event_type: str,
    session_id: Any,
    key: str,
    payload: dict[str, Any] | None = None,
    turn_id: str = "",
    call_id: str = "",
    attempt: int | None = None,
    model: str = "",
    prompt_version: str = "",
    policy_version: str = "",
) -> bool:
    """写一条观测（幂等、旁路）；返回是否真正插入。

    `key` 是同一逻辑事实的稳定业务键（例如 `turn:call:attempt`）——重复调用只写一次。
    """
    if event_type not in EVENT_TYPES:  # pragma: no cover - 调用方都是常量
        logger.warning("未知观测类型，忽略：%s", event_type)
        return False
    identifier = event_id(event_type, str(session_id), str(key))
    statement = (
        insert(ContextObservation)
        .values(
            event_id=identifier,
            session_id=session_id,
            event_type=event_type,
            turn_id=turn_id or None,
            call_id=call_id or None,
            attempt=attempt,
            model=model or None,
            prompt_version=prompt_version or None,
            policy_version=policy_version or None,
            payload=sanitize(payload),
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
    )
    try:
        async with session.begin_nested():
            result = cast(CursorResult[Any], await session.execute(statement))
        return bool(result.rowcount)
    except Exception:  # noqa: BLE001 - 观测是旁路，绝不阻断业务
        logger.warning("观测写入失败（不阻断业务）：%s %s", event_type, key, exc_info=True)
        return False


async def record_detached(
    factory: Any,
    *,
    event_type: str,
    session_id: Any,
    key: str,
    payload: dict[str, Any] | None = None,
    **fields: Any,
) -> bool:
    """用独立会话写一条观测（最安全的旁路方式：不参与调用方事务）。"""
    try:
        async with factory() as session:
            written = await record(
                session, event_type=event_type, session_id=session_id, key=key,
                payload=payload, **fields,
            )
            await session.commit()
            return written
    except Exception:  # noqa: BLE001
        logger.warning("观测写入失败（独立会话，不阻断业务）：%s", event_type, exc_info=True)
        return False


def payload_digest(payload: dict[str, Any]) -> str:
    """payload 的稳定摘要（报告里用于核对同一事实）。"""
    return hashlib.sha256(
        json.dumps(sanitize(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
