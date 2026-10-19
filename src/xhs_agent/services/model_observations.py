"""把实际传输生命周期以独立事务落库；传输前 started 已持久化，进程退出仍可对账。"""
from __future__ import annotations

from typing import Any

from ..config import AppConfig
from ..core.model_attempts import Observer
from ..core.observations import record_detached


def observer(factory: Any, cfg: AppConfig, *, session_id: str, turn_id: str = "",
             run_id: str = "", policy: str = "",
             interrupted_records: list[dict[str, Any]] | None = None) -> Observer:
    recorded: set[str] = set()
    async def record(phase: str, facts: dict[str, Any]) -> None:
        body = {**facts, "run_id": run_id}
        price = dict(body.get("price") or {})
        # 前缀折扣价格只用于同一文本端点；视觉端点不能套用文本价格。
        if body.get("provider_kind") == "text":
            price.update(cache_hit_per_m=cfg.context.price_cache_hit_per_m,
                         cache_miss_per_m=cfg.context.price_cache_miss_per_m,
                         price_version=cfg.context.price_cache_version or "config")
        body["price"] = price
        identity = f"{facts['identity']}:{facts['attempt']}"
        if phase == "finished" and facts.get("status") == "interrupted" \
                and interrupted_records is not None and identity not in recorded:
            recorded.add(identity)
            interrupted_records.append({**body, "usage_status": body.get("usage_status", "unknown"),
                                         "error": "cancelled"})
        await record_detached(factory, event_type=f"model_attempt_{phase}",
            session_id=session_id, key=f"{identity}:{phase}",
            payload=body, turn_id=turn_id, call_id=str(facts["identity"]),
            attempt=int(facts["attempt"]), model=str(facts.get("model") or ""),
            prompt_version=str(facts.get("prompt_version") or ""),
            policy_version=policy or "attempt_v3")
    return record
