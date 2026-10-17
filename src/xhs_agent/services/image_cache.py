"""图片解析的持久复用（S7.27）：内容身份、查询、写入、消费与维护。

用途：同一会话里同一组图（内容、顺序、模型、参数、prompt、schema 全都一致）只真正调用一次
多模态接口；跨轮次、刷新与进程重启后仍能复用。缓存是**旁路对象**：到期 / 清理只影响复用，
不动用户确认或编辑过的线索版本（那属于 `chat_artifact_versions`）。
输入：附件（路径 / 摘要）、`[vision]` 与 `[context]` 配置、持久层会话。
输出：`CacheLookup`（H/M/E/B + 层级 + 原因 + 耗时）与成功解析 payload。

口径（方案 §8）：

- 键 = 会话 + 有序图片内容摘要 + 供应商/端点/模型 + 参数摘要 + prompt 摘要与版本 + schema 版本；
- **先校验归属与存在性，再查缓存**：摘要不能替代访问校验；
- 一次逻辑查询只写**一条**主结果（轮内命中就不再查库，也不重复记 miss）；
- 失败 / 取消 / 部分解析**不写**成功缓存；命中不续期；创建后 `[context].image_cache_ttl_days` 失效；
- 同键并发首次 miss 允许各付一次，唯一约束只保留一个有效条目——不声称"恰好一次"。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..core.observations import record as record_observation
from ..db.models import ChatImageParseCache
from ..services.chat import resolve_attachment
from ..services.image_clue import MULTI_TASK_ID
from ..services.image_clue import TASK_ID as SINGLE_TASK_ID
from ..tools import prompt as prompt_tool

logger = logging.getLogger("xhs_agent.image_cache")

#: 缓存身份里的 schema 版本：解析结果结构变化时递增（旧条目自然失配）
SCHEMA_VERSION = "image_clue_v1"
#: 查询结论（与报告口径一致）
OUTCOMES = ("hit", "miss", "error", "bypass")

Reader = Callable[[str], Awaitable[bytes]]


def digest_bytes(data: bytes) -> str:
    """图片内容摘要（sha256）；不依赖路径，路径迁移不改变内容身份。"""
    return hashlib.sha256(data).hexdigest()


def prompt_identity(task_id: str) -> tuple[str, int]:
    """prompt 资产的 (摘要, 版本)：内容一变摘要就变，旧缓存自然失配。"""
    loaded = prompt_tool.load(task_id)
    body = "\n".join(loaded.sections[key] for key in loaded.sections)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16], int(loaded.version)


def build_identity(
    cfg: AppConfig, digests: list[str], *, multi: bool | None = None
) -> dict[str, Any]:
    """构造完整身份；`multi` 缺省按图片张数推断（1 张 = 单图 prompt）。"""
    task_id = MULTI_TASK_ID if (multi if multi is not None else len(digests) > 1) else SINGLE_TASK_ID
    prompt_digest, prompt_version = prompt_identity(task_id)
    params = {
        "task_id": task_id,
        "max_width": int(cfg.vision.max_width),
        "timeout_s": int(cfg.vision.timeout_s),
    }
    return {
        "image_digests": list(digests),
        "provider": "vision",
        "base_url": str(cfg.vision.base_url or "").rstrip("/"),
        "model": str(cfg.vision.model or ""),
        "params_digest": hashlib.sha256(
            json.dumps(params, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16],
        "prompt_digest": prompt_digest,
        "prompt_version": prompt_version,
        "schema_version": SCHEMA_VERSION,
    }


def cache_key(identity: dict[str, Any]) -> str:
    """身份 → 缓存键（规范化 JSON 的 sha256）。"""
    body = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass
class CacheLookup:
    """一次逻辑查询的结果（写一条 `cache_lookup_finished`）。"""

    outcome: str                  # hit / miss / error / bypass
    level: str = "none"           # turn / persistent / none
    reason: str = ""              # no_entry / expired / read_error / disabled / ...
    payload: dict[str, Any] | None = None
    key: str = ""
    elapsed_ms: int = 0
    cross_turn: bool = False
    identity: dict[str, Any] = field(default_factory=dict)

    @property
    def hit(self) -> bool:
        return self.outcome == "hit"


async def _default_reader(cfg: AppConfig, session_id: str, path: str) -> bytes:
    resolved = resolve_attachment(cfg, session_id, path)
    return await asyncio.to_thread(Path(resolved).read_bytes)


async def ensure_digests(
    cfg: AppConfig,
    session_id: str,
    attachments: list[dict[str, Any]],
    *,
    reader: Reader | None = None,
) -> tuple[list[str], int, list[str]]:
    """确保每个附件都有内容摘要；旧附件首次使用时补算并记录耗时。

    返回（有序摘要, 补算耗时毫秒, 读取失败的附件名）。**摘要只用于身份**——归属与存在性
    仍由 `resolve_attachment` 校验（读不到就是失败，不会命中缓存）。
    """
    digests: list[str] = []
    elapsed = 0
    failures: list[str] = []
    for attachment in attachments:
        existing = str(attachment.get("sha256") or "")
        if existing:
            digests.append(existing)
            continue
        path = str(attachment.get("path") or "")
        started = time.monotonic()
        try:
            data = await (reader(path) if reader is not None
                          else _default_reader(cfg, str(session_id), path))
        except Exception:  # noqa: BLE001 - 读不到 = 这批图不能用，由调用方按失败处理
            failures.append(str(attachment.get("name") or path))
            digests.append("")
            continue
        elapsed += int((time.monotonic() - started) * 1000)
        digest = digest_bytes(data)
        attachment["sha256"] = digest
        digests.append(digest)
    return digests, elapsed, failures


async def lookup(
    session: AsyncSession,
    cfg: AppConfig,
    *,
    session_id: Any,
    identity: dict[str, Any],
    turn_payload: dict[str, Any] | None = None,
    turn_id: str = "",
    call_id: str = "",
) -> CacheLookup:
    """一次逻辑查询：轮内 → 持久层；只写一条主结果。

    `turn_payload` 是同一轮里已经解析过的结果（`ToolCache`）——命中就不再查库，
    也不重复记 miss（方案 §8.1）。
    """
    key = cache_key(identity)
    started = time.monotonic()
    if turn_payload is not None:
        result = CacheLookup(
            outcome="hit", level="turn", reason="turn_cache", payload=turn_payload,
            key=key, elapsed_ms=int((time.monotonic() - started) * 1000),
            identity=identity,
        )
        await _observe(cfg, session, session_id, result, turn_id=turn_id, call_id=call_id)
        return result
    if not cfg.context.image_cache_enabled:
        result = CacheLookup(
            outcome="bypass", level="none", reason="disabled", key=key,
            elapsed_ms=int((time.monotonic() - started) * 1000), identity=identity,
        )
        await _observe(cfg, session, session_id, result, turn_id=turn_id, call_id=call_id)
        return result
    try:
        row = (
            await session.execute(
                select(ChatImageParseCache).where(
                    ChatImageParseCache.session_id == session_id,
                    ChatImageParseCache.cache_key == key,
                )
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - 读失败是 E，不是 miss
        logger.warning("图片缓存读取失败（按 E 记录，不当作 miss）", exc_info=True)
        await session.rollback()
        result = CacheLookup(
            outcome="error", level="persistent", reason="read_error", key=key,
            elapsed_ms=int((time.monotonic() - started) * 1000), identity=identity,
        )
        await _observe(cfg, session, session_id, result, turn_id=turn_id, call_id=call_id)
        return result
    elapsed = int((time.monotonic() - started) * 1000)
    if row is None:
        result = CacheLookup(
            outcome="miss", level="persistent", reason="no_entry", key=key,
            elapsed_ms=elapsed, identity=identity,
        )
        await _observe(cfg, session, session_id, result, turn_id=turn_id, call_id=call_id)
        return result
    now = datetime.now(UTC)
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    if expires <= now:
        result = CacheLookup(
            outcome="miss", level="persistent", reason="expired", key=key,
            elapsed_ms=elapsed, identity=identity,
        )
        await _observe(cfg, session, session_id, result, turn_id=turn_id, call_id=call_id)
        return result
    payload = dict(row.payload or {})
    source = dict(row.source or {})
    result = CacheLookup(
        outcome="hit", level="persistent", reason="", payload=payload, key=key,
        elapsed_ms=elapsed, identity=identity,
        cross_turn=bool(str(source.get("turn_id") or "") not in ("", str(turn_id))),
    )
    await _observe(cfg, session, session_id, result, turn_id=turn_id, call_id=call_id)
    return result


async def store(
    session: AsyncSession,
    cfg: AppConfig,
    *,
    session_id: Any,
    identity: dict[str, Any],
    payload: dict[str, Any],
    turn_id: str = "",
    message_id: str = "",
    order: int = 0,
) -> bool:
    """写一条成功缓存（唯一约束冲突时忽略）；返回是否真正写入。

    写失败可以返回本次有效解析（调用方继续用），但**必须**标缓存存储错误，
    不能宣称已持久复用。
    """
    if not cfg.context.image_cache_enabled:
        return False
    key = cache_key(identity)
    now = datetime.now(UTC)
    statement = (
        insert(ChatImageParseCache)
        .values(
            session_id=session_id,
            cache_key=key,
            identity=dict(identity),
            payload=dict(payload),
            source={"turn_id": turn_id, "message_id": message_id, "order": order},
            created_at=now,
            expires_at=now + timedelta(days=int(cfg.context.image_cache_ttl_days)),
        )
        .on_conflict_do_nothing(constraint="uq_chat_image_parse_cache_session_key")
    )
    try:
        cursor = cast(CursorResult[Any], await session.execute(statement))
        await session.commit()
        return bool(cursor.rowcount)
    except Exception:  # noqa: BLE001 - 缓存写失败不阻断本次解析
        logger.warning("图片缓存写入失败（本次解析仍可用，但不宣称已复用）", exc_info=True)
        await session.rollback()
        return False


async def consume(
    session: AsyncSession,
    *,
    session_id: Any,
    lookup_result: CacheLookup,
    turn_id: str = "",
    call_id: str = "",
    used_for: str = "image_clue",
) -> None:
    """`artifact_consumed`：缓存结果被任务**实际使用**（命中但随后取消不算消费）。"""
    if not lookup_result.hit:
        return
    await record_observation(
        session,
        event_type="artifact_consumed",
        session_id=session_id,
        key=f"{turn_id}:{call_id}:{lookup_result.key}:{used_for}",
        payload={
            "cache_level": lookup_result.level,
            "cache_key": lookup_result.key,
            "cross_turn": bool(lookup_result.cross_turn),
            "new_visual_calls": 0,
            "used_for": used_for,
        },
        turn_id=turn_id,
        call_id=call_id,
        policy_version=SCHEMA_VERSION,
    )


async def purge_expired(
    session: AsyncSession, *, dry_run: bool = False, now: datetime | None = None
) -> int:
    """删除到期条目（每日维护）；`dry_run` 只统计不删。"""
    moment = now or datetime.now(UTC)
    if dry_run:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(ChatImageParseCache).where(
                        ChatImageParseCache.expires_at <= moment
                    )
                )
            ).scalar_one()
        )
    cursor = cast(
        CursorResult[Any],
        await session.execute(
            delete(ChatImageParseCache).where(ChatImageParseCache.expires_at <= moment)
        ),
    )
    await session.commit()
    return int(cursor.rowcount or 0)


async def purge_session(session: AsyncSession, session_id: Any) -> int:
    """清掉某会话的全部缓存条目（附件归属失效时用）；返回删除条数。"""
    cursor = cast(
        CursorResult[Any],
        await session.execute(
            delete(ChatImageParseCache).where(ChatImageParseCache.session_id == session_id)
        ),
    )
    await session.commit()
    return int(cursor.rowcount or 0)


async def stats(session: AsyncSession) -> dict[str, int]:
    """维护命令用的计数：总条目 / 已到期 / 会话数。"""
    total = int(
        (await session.execute(select(func.count()).select_from(ChatImageParseCache)))
        .scalar_one()
    )
    expired = await purge_expired(session, dry_run=True)
    sessions = int(
        (
            await session.execute(
                select(func.count(func.distinct(ChatImageParseCache.session_id)))
            )
        ).scalar_one()
    )
    return {"total": total, "expired": expired, "sessions": sessions}


async def _observe(
    cfg: AppConfig,
    session: AsyncSession,
    session_id: Any,
    result: CacheLookup,
    *,
    turn_id: str,
    call_id: str,
) -> None:
    await record_observation(
        session,
        event_type="cache_lookup_finished",
        session_id=session_id,
        key=f"{turn_id}:{call_id}:{result.key}",
        payload={
            "outcome": result.outcome,
            "level": result.level,
            "reason": result.reason,
            "elapsed_ms": result.elapsed_ms,
            "cache_key": result.key,
            "images": len(result.identity.get("image_digests") or []),
            "prompt_version": result.identity.get("prompt_version"),
            "schema_version": result.identity.get("schema_version"),
            "cross_turn": bool(result.cross_turn),
        },
        turn_id=turn_id,
        call_id=call_id,
        prompt_version=f"image_cache:v{result.identity.get('prompt_version')}",
        policy_version=str(cfg.context.mode),
    )


async def record_turn_hit(
    session: AsyncSession,
    *,
    session_id: Any,
    key: str,
    turn_id: str = "",
    call_id: str = "",
    images: int = 0,
) -> None:
    """轮内缓存命中：只写一条主结果（不再查库，也不重复记 miss）。"""
    await record_observation(
        session,
        event_type="cache_lookup_finished",
        session_id=session_id,
        key=f"{turn_id}:{call_id}:{key}",
        payload={
            "outcome": "hit",
            "level": "turn",
            "reason": "turn_cache",
            "elapsed_ms": 0,
            "cache_key": key,
            "images": int(images),
            "cross_turn": False,
        },
        turn_id=turn_id,
        call_id=call_id,
        policy_version="turn_cache",
    )
