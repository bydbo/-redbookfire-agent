"""上下文与缓存度量复算脚本（S7.28）。

用途：从 `context_observations`（本地事实源）复算应用缓存与供应商前缀缓存指标，输出 JSON 与
Markdown；保留原始分子 / 分母、过滤条件、未知数与价格版本，**缺失不填零、零分母输出 N/A**。
输入：`--since/--until`（ISO 日期或时间）、`--model`、`--prompt`、`--policy`、`--out-dir`。
输出：`<out-dir>/context-metrics-<时间>.json` 与同名 `.md`；不写库、不改业务数据。

口径（方案 §9）：

- 应用缓存：H / M / E / B 来自 `cache_lookup_finished`；C = H+M+E+B（有效逻辑请求）；
  入口不合法且未受理的请求不在本表（没有对应观测）；
- 跨轮复用率 = 跨轮命中的 `artifact_consumed` / C；命中消费率 = 被使用的命中 / H；
- 条目复用率 = 窗口内创建且被再次消费的有效条目 / 窗口内创建的有效条目（注明创建队列与截止日）；
- 前缀缓存：只有 `cache_status=complete` 的传输进入 K；`hit+miss=input` 由写入端保证；
  未知分项比例 = 支持请求中缺失 / 非法 / 冲突数 / 支持请求数；
- 费用：分项完整且有价格时才给 `estimated_prefix_saving`，否则 `unknown`；
- 只聚合**叶子**观测（每次实际传输 / 每次逻辑查询），不与轮次或工具汇总相加。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xhs_agent.config import AppConfig, load_config
from xhs_agent.db.models import ChatImageParseCache, ContextObservation
from xhs_agent.db.session import create_engine_from_config, create_session_factory

APP_CACHE_EVENTS = ("cache_lookup_finished",)
CONSUMED_EVENTS = ("artifact_consumed",)
ATTEMPT_EVENTS = ("model_attempt_finished",)


def _rate(numerator: float | int | None, denominator: float | int | None) -> Any:
    """零分母输出 `N/A`（字符串），不把未知写成 0。"""
    if not denominator:
        return "N/A"
    if numerator is None:
        return "N/A"
    return round(float(numerator) / float(denominator), 4)


@dataclass
class AppCacheMetrics:
    hits: int = 0
    misses: int = 0
    errors: int = 0
    bypasses: int = 0
    turn_hits: int = 0
    persistent_hits: int = 0
    miss_reasons: dict[str, int] = field(default_factory=dict)
    consumed_hits: int = 0
    cross_turn_consumed: int = 0
    created_entries: int = 0
    reused_entries: int = 0

    @property
    def logical_requests(self) -> int:
        return self.hits + self.misses + self.errors + self.bypasses

    def as_dict(self) -> dict[str, Any]:
        c = self.logical_requests
        return {
            "H": self.hits,
            "M": self.misses,
            "E": self.errors,
            "B": self.bypasses,
            "C": c,
            "layer": {"turn": self.turn_hits, "persistent": self.persistent_hits},
            "primary_hit_rate": _rate(self.hits, self.hits + self.misses + self.errors),
            "normal_hit_rate": _rate(self.hits, self.hits + self.misses),
            "cross_turn_reuse_rate": _rate(self.cross_turn_consumed, c),
            "hit_consumption_rate": _rate(self.consumed_hits, self.hits),
            "entry_reuse_rate": _rate(self.reused_entries, self.created_entries),
            "entry_queue": {
                "created_in_window": self.created_entries,
                "reused_in_window": self.reused_entries,
                "note": "分子只算窗口内创建、窗口内再次消费的条目；窗口外创建的命中不计入分子",
            },
            "bypass_rate": _rate(self.bypasses, c),
            "error_rate": _rate(self.errors, self.hits + self.misses + self.errors),
            "miss_reasons": dict(sorted(self.miss_reasons.items())),
        }


@dataclass
class PrefixMetrics:
    attempts: int = 0
    supported_attempts: int = 0
    complete: int = 0
    unknown: int = 0
    input_tokens: int = 0
    hit_tokens: int = 0
    miss_tokens: int = 0
    hit_requests: int = 0
    complete_output_tokens: int = 0

    def savings(self, cfg: AppConfig) -> dict[str, Any]:
        hit_price = cfg.context.price_cache_hit_per_m
        miss_price = cfg.context.price_cache_miss_per_m
        meta = {
            "currency": cfg.context.price_cache_currency,
            "price_version": cfg.context.price_cache_version,
        }
        if hit_price is None or miss_price is None:
            return {"estimated_prefix_saving": "unknown", "reason": "price_missing", **meta}
        if self.complete == 0:
            return {"estimated_prefix_saving": "unknown", "reason": "no_complete_samples",
                    **meta}
        saving = self.hit_tokens * (miss_price - hit_price) / 1_000_000
        return {
            "estimated_prefix_saving": round(saving, 6),
            "reason": "",
            **meta,
            "basis": {
                "hit_tokens": self.hit_tokens,
                "price_miss": miss_price,
                "price_hit": hit_price,
            },
        }

    def as_dict(self, cfg: AppConfig) -> dict[str, Any]:
        return {
            "K": self.complete,
            "attempts": self.attempts,
            "supported_attempts": self.supported_attempts,
            "unknown": self.unknown,
            "input_tokens": self.input_tokens,
            "hit_tokens": self.hit_tokens,
            "miss_tokens": self.miss_tokens,
            "token_hit_rate": _rate(self.hit_tokens, self.input_tokens),
            "supported_complete_coverage": _rate(self.complete, self.supported_attempts),
            "overall_observable_coverage": _rate(self.complete, self.attempts),
            "request_hit_rate": _rate(self.hit_requests, self.complete),
            "unknown_ratio": _rate(self.unknown, self.supported_attempts),
            "cost": self.savings(cfg),
        }


async def _collect(
    session: AsyncSession,
    cfg: AppConfig,
    *,
    since: datetime | None,
    until: datetime | None,
    model: str,
    prompt: str,
    policy: str,
) -> tuple[AppCacheMetrics, PrefixMetrics, dict[str, Any]]:
    app = AppCacheMetrics()
    prefix = PrefixMetrics()
    filters: dict[str, Any] = {
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "model": model or None,
        "prompt": prompt or None,
        "policy": policy or None,
    }

    def _match(row: ContextObservation) -> bool:
        if since and row.created_at and row.created_at < since:
            return False
        if until and row.created_at and row.created_at > until:
            return False
        if model and str(row.model or "") != model:
            return False
        if prompt and str(row.prompt_version or "") != prompt:
            return False
        return not (policy and str(row.policy_version or "") != policy)

    rows = list((await session.execute(select(ContextObservation))).scalars().all())
    consumed_keys: set[str] = set()
    for row in rows:
        if not _match(row):
            continue
        payload = dict(row.payload or {})
        if row.event_type in APP_CACHE_EVENTS:
            outcome = str(payload.get("outcome") or "")
            level = str(payload.get("level") or "")
            if outcome == "hit":
                app.hits += 1
                if level == "turn":
                    app.turn_hits += 1
                else:
                    app.persistent_hits += 1
            elif outcome == "miss":
                app.misses += 1
                reason = str(payload.get("reason") or "unknown")
                app.miss_reasons[reason] = app.miss_reasons.get(reason, 0) + 1
            elif outcome == "error":
                app.errors += 1
                app.miss_reasons["read_error"] = app.miss_reasons.get("read_error", 0) + 1
            elif outcome == "bypass":
                app.bypasses += 1
            key = str(payload.get("cache_key") or "")
            if key and outcome == "miss":
                consumed_keys.add(key)
        elif row.event_type in CONSUMED_EVENTS:
            app.consumed_hits += 1
            if bool(payload.get("cross_turn")):
                app.cross_turn_consumed += 1
            key = str(payload.get("cache_key") or "")
            if key:
                consumed_keys.add(key)
        elif row.event_type in ATTEMPT_EVENTS:
            prefix.attempts += 1
            status = str(payload.get("cache_status") or "")
            if status == "complete":
                prefix.complete += 1
                prefix.supported_attempts += 1
                hit = int(payload.get("cache_hit_tokens") or 0)
                miss = int(payload.get("cache_miss_tokens") or 0)
                prefix.hit_tokens += hit
                prefix.miss_tokens += miss
                prefix.input_tokens += hit + miss
                prefix.complete_output_tokens += int(payload.get("completion_tokens") or 0)
                if hit > 0:
                    prefix.hit_requests += 1
            elif status in ("incomplete",):
                prefix.supported_attempts += 1
                prefix.unknown += 1

    # 条目复用率：只看**本窗口创建**的条目
    cache_rows = list((await session.execute(select(ChatImageParseCache))).scalars().all())
    for row in cache_rows:
        created = row.created_at
        if since and created and created < since:
            continue
        if until and created and created > until:
            continue
        app.created_entries += 1
        if str(row.cache_key) in consumed_keys:
            app.reused_entries += 1

    totals = {
        "observations": len(rows),
        "events": {
            "cache_lookup_finished": sum(
                1 for row in rows if row.event_type in APP_CACHE_EVENTS and _match(row)
            ),
            "artifact_consumed": sum(
                1 for row in rows if row.event_type in CONSUMED_EVENTS and _match(row)
            ),
            "model_attempt_finished": prefix.attempts,
        },
        "unknown_note": "缺失 / 非法 / 冲突的缓存分项计入 unknown，不填零",
    }
    return app, prefix, {"filters": filters, "totals": totals}


def _render_markdown(report: dict[str, Any]) -> str:
    app = report["app_cache"]
    prefix = report["prefix_cache"]
    lines = ["# 上下文与缓存度量报告", ""]
    lines.append(f"生成时间：{report['generated_at']}")
    lines.append(f"过滤条件：`{json.dumps(report['filters'], ensure_ascii=False)}`")
    lines.append("")
    lines.append("## 应用缓存")
    lines.append("")
    lines.append(f"- H/M/E/B = {app['H']} / {app['M']} / {app['E']} / {app['B']}，C = {app['C']}")
    lines.append(f"- 主自动命中率 H/(H+M+E) = {app['primary_hit_rate']}")
    lines.append(f"- 辅助正常命中率 H/(H+M) = {app['normal_hit_rate']}")
    lines.append(f"- 跨轮复用率 = {app['cross_turn_reuse_rate']}")
    lines.append(f"- 命中消费率 = {app['hit_consumption_rate']}")
    lines.append(f"- 条目复用率 = {app['entry_reuse_rate']}（{app['entry_queue']['note']}）")
    lines.append(f"- 绕过率 = {app['bypass_rate']}；缓存错误率 = {app['error_rate']}")
    lines.append(f"- miss 原因分布：{json.dumps(app['miss_reasons'], ensure_ascii=False)}")
    lines.append("")
    lines.append("## 供应商前缀缓存")
    lines.append("")
    lines.append(f"- K（完整分项请求数）= {prefix['K']}；实际传输 = {prefix['attempts']}")
    lines.append(f"- token 命中率 = {prefix['token_hit_rate']}")
    lines.append(f"- 支持请求完整覆盖率 = {prefix['supported_complete_coverage']}")
    lines.append(f"- 总体可观测覆盖率 = {prefix['overall_observable_coverage']}")
    lines.append(f"- 请求命中率 = {prefix['request_hit_rate']}；未知分项比例 = {prefix['unknown_ratio']}")
    cost = prefix["cost"]
    lines.append(
        f"- 前缀节省估算 = {cost.get('estimated_prefix_saving')}"
        f"（币种 {cost.get('currency')}，价格版本 {cost.get('price_version') or '未登记'}）"
    )
    lines.append("")
    lines.append("## 样本与缺口")
    lines.append("")
    lines.append(f"- 观测总数：{report['totals']['observations']}")
    lines.append(f"- 事件计数：{json.dumps(report['totals']['events'], ensure_ascii=False)}")
    lines.append(f"- 未知口径：{report['totals']['unknown_note']}")
    lines.append("")
    lines.append("> 本报告只聚合叶子观测；不与轮次 / 工具汇总相加。零分母输出 N/A，不做推测。")
    return "\n".join(lines)


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config()
    since = _parse_dt(args.since)
    until = _parse_dt(args.until)
    engine = create_engine_from_config(cfg)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            app, prefix, extra = await _collect(
                session, cfg, since=since, until=until, model=args.model,
                prompt=args.prompt, policy=args.policy,
            )
    finally:
        await engine.dispose()
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        **extra,
        "app_cache": app.as_dict(),
        "prefix_cache": prefix.as_dict(cfg),
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"context-metrics-{stamp}.json"
    md_path = out_dir / f"context-metrics-{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False))
    return 0


def _parse_dt(raw: str | None) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def main() -> int:
    import asyncio

    parser = argparse.ArgumentParser(description="复算上下文与缓存度量并导出 JSON/Markdown")
    parser.add_argument("--since", default="", help="起始时间（ISO 8601）")
    parser.add_argument("--until", default="", help="结束时间（ISO 8601）")
    parser.add_argument("--model", default="", help="只看某个模型")
    parser.add_argument("--prompt", default="", help="只看某个 prompt 版本标识")
    parser.add_argument("--policy", default="", help="只看某个策略版本")
    parser.add_argument("--out-dir", default="runs/metrics", help="输出目录（runs/ 已忽略）")
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
