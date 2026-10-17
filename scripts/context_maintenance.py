"""上下文与缓存维护命令（S7.27 / S7.28）：清理到期缓存与超期观测。

用法：

    uv run python scripts/context_maintenance.py --dry-run
    uv run python scripts/context_maintenance.py
    uv run python scripts/context_maintenance.py --json

做两件事（都是**删除型**，默认先跑 dry-run）：

1. 删 `chat_image_parse_cache` 里 `expires_at <= now()` 的条目（TTL 创建后 7 天，命中不续期）；
2. 删 `context_observations` 里超过 `[context].observation_retention_days`（默认 30 天）的事件。

**不删除**业务状态、产物版本与既有业务账目。业务审计（`chat_context_events`）与
`chat_artifact_versions` 不受本命令影响——回退靠关闭开关，不靠删历史。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from xhs_agent.config import AppConfig, load_config
from xhs_agent.db.models import ContextObservation
from xhs_agent.db.session import create_engine_from_config, create_session_factory
from xhs_agent.services import image_cache


async def _observation_stats(session: AsyncSession, cfg: AppConfig) -> dict[str, int]:
    cutoff = datetime.now(UTC) - timedelta(days=int(cfg.context.observation_retention_days))
    total = int(
        (await session.execute(select(func.count()).select_from(ContextObservation)))
        .scalar_one()
    )
    expired = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ContextObservation)
                .where(ContextObservation.created_at < cutoff)
            )
        ).scalar_one()
    )
    return {"total": total, "expired": expired}


async def _purge_observations(session: AsyncSession, cfg: AppConfig) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=int(cfg.context.observation_retention_days))
    result = await session.execute(
        delete(ContextObservation).where(ContextObservation.created_at < cutoff)
    )
    await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


async def _run(cfg: AppConfig, *, dry_run: bool) -> dict[str, Any]:
    engine = create_engine_from_config(cfg)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            cache_stats = await image_cache.stats(session)
            obs_stats = await _observation_stats(session, cfg)
            if dry_run:
                return {
                    "dry_run": True,
                    "image_cache": cache_stats,
                    "observations": obs_stats,
                    "deleted": {"image_cache": 0, "observations": 0},
                }
            cache_deleted = await image_cache.purge_expired(session)
            obs_deleted = await _purge_observations(session, cfg)
            return {
                "dry_run": False,
                "image_cache": cache_stats,
                "observations": obs_stats,
                "deleted": {"image_cache": cache_deleted, "observations": obs_deleted},
            }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="清理到期图片缓存与超期上下文观测")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不删除")
    parser.add_argument("--json", action="store_true", help="输出 JSON（默认也是 JSON）")
    args = parser.parse_args()
    cfg = load_config()
    summary = asyncio.run(_run(cfg, dry_run=bool(args.dry_run)))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
