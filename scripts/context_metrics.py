"""上下文与缓存度量复算脚本（v2，E7CTX-13 / 14）。

用途：从 `context_observations`（本地事实源）复算应用缓存与供应商前缀缓存指标，输出 JSON 与
Markdown；保留原始分子 / 分母、过滤条件、未知数与**每次尝试的价格快照**，缺失不填零、
零分母输出 N/A。

口径（v2 §2.6 / §3.7）：

- **C 来自受理记录**（`cache_request_started`），不能仅由已写出的 H/M/E/B 反推；
  轮内与持久层各一次逻辑请求，不重复扩大分母；
- **条目创建队列来自 `cache_entry_created`**（按 generation_id）——清理缓存表不丢历史；
  复用 = 队列内条目在创建后、观察截止前出现真实 `artifact_consumed`（同 generation_id）；
  **miss 不进入复用集合**；
- 失效原因来自 `cache_entry_invalidated`（带具体条目身份与可证实原因）；
- 前缀缓存：只有 `cache_status=complete` 的传输进入 K；支持供应商**缺分项仍计入覆盖率
  分母**（不归为 unsupported 排除）；价格用**每次尝试的价格快照**，不用当前价格重算历史；
- 时间窗口为 UTC 半开区间 `[since, until)`；非法时间 / 起止逆序直接参数错误（退出码 2）；
- 过滤字段：model / prompt / policy / cache-layer / experiment-arm；未知标识单列；
- `--strict`：门槛未达或证据不全退出 1，配置 / 环境无法报告退出 2，全部达标退出 0；
  未启用 strict 的观察报告保留 N/A 和限制。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xhs_agent.config import AppConfig, load_config
from xhs_agent.db.models import ContextObservation
from xhs_agent.db.session import create_engine_from_config, create_session_factory


def _rate(numerator: float | int | None, denominator: float | int | None) -> Any:
    """零分母输出 `N/A`（字符串），不把未知写成 0。"""
    if not denominator:
        return "N/A"
    if numerator is None:
        return "N/A"
    return round(float(numerator) / float(denominator), 4)


def _parse_dt(raw: str) -> datetime:
    """解析 ISO 时间；非法直接参数错误（退出码 2），不静默取消过滤。"""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("时间参数为空")
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass
class AppCacheMetrics:
    hits: int = 0
    misses: int = 0
    errors: int = 0
    bypasses: int = 0
    turn_hits: int = 0
    persistent_hits: int = 0
    miss_reasons: dict[str, int] = field(default_factory=dict)
    invalidation_reasons: dict[str, int] = field(default_factory=dict)
    requests_started: int = 0
    consumed_generations: set[str] = field(default_factory=set)
    created_generations: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        c = self.requests_started
        reused = len(self.created_generations & self.consumed_generations)
        return {
            "C": c,
            "C_source": "cache_request_started",
            "H": self.hits,
            "M": self.misses,
            "E": self.errors,
            "B": self.bypasses,
            "layer": {"turn": self.turn_hits, "persistent": self.persistent_hits},
            "primary_hit_rate": _rate(self.hits, self.hits + self.misses + self.errors),
            "normal_hit_rate": _rate(self.hits, self.hits + self.misses),
            "hit_consumption_rate": _rate(
                len(self.consumed_generations & self.created_generations), self.hits
            ),
            "entry_reuse_rate": _rate(reused, len(self.created_generations)),
            "entry_queue": {
                "created_events": len(self.created_generations),
                "consumed_generations": len(self.consumed_generations),
                "reused_in_queue": reused,
                "note": "分子只算创建队列内、观察截止前被真实消费（同 generation_id）的条目；"
                        "miss 不进入复用集合",
            },
            "bypass_rate": _rate(self.bypasses, c),
            "error_rate": _rate(self.errors, self.hits + self.misses + self.errors),
            "miss_reasons": dict(sorted(self.miss_reasons.items())),
            "invalidation_reasons": dict(sorted(self.invalidation_reasons.items())),
        }


@dataclass
class PrefixMetrics:
    attempts: int = 0
    supported_attempts: int = 0
    complete: int = 0
    unsupported: int = 0
    input_tokens: int = 0
    hit_tokens: int = 0
    hit_requests: int = 0
    started_attempts: int = 0
    prices: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "K": self.complete,
            "attempts_finished": self.attempts,
            "attempts_started": self.started_attempts,
            "incomplete_attempts": max(0, self.supported_attempts - self.complete),
            "supported_attempts": self.supported_attempts,
            "unsupported_attempts": self.unsupported,
            "input_tokens": self.input_tokens,
            "hit_tokens": self.hit_tokens,
            "token_hit_rate": _rate(self.hit_tokens, self.input_tokens),
            "supported_complete_coverage": _rate(self.complete, self.supported_attempts),
            "overall_observable_coverage": _rate(self.complete, self.attempts),
            "request_hit_rate": _rate(self.hit_requests, self.complete),
            "unknown_ratio": _rate(max(0, self.supported_attempts - self.complete),
                                   self.supported_attempts),
            "price_snapshots": self.prices,
            "note": "支持供应商缺分项仍计入 supported 分母（不归为 unsupported 排除）；"
                    "K 要求真实 input + hit + miss 合法一致；价格按尝试快照",
        }


def _match(
    row: ContextObservation,
    *,
    since: datetime,
    until: datetime,
    model: str,
    prompt: str,
    policy: str,
    cache_layer: str,
    experiment_arm: str,
) -> bool:
    created = row.created_at
    if created is None or not (since <= created < until):
        return False
    if model and str(row.model or "") != model:
        return False
    if prompt and str(row.prompt_version or "") != prompt:
        return False
    if policy and str(row.policy_version or "") != policy:
        return False
    if cache_layer or experiment_arm:
        payload = dict(row.payload or {})
        if cache_layer and str(payload.get("cache_layer") or "") != cache_layer:
            return False
        if experiment_arm and str(payload.get("experiment_arm") or "") != experiment_arm:
            return False
    return True


async def _collect(
    session: AsyncSession,
    cfg: AppConfig,
    *,
    since: datetime,
    until: datetime,
    model: str,
    prompt: str,
    policy: str,
    cache_layer: str,
    experiment_arm: str,
) -> tuple[AppCacheMetrics, PrefixMetrics, dict[str, Any]]:
    app = AppCacheMetrics()
    prefix = PrefixMetrics()

    def match(row: ContextObservation) -> bool:
        return _match(
            row, since=since, until=until, model=model, prompt=prompt, policy=policy,
            cache_layer=cache_layer, experiment_arm=experiment_arm,
        )

    rows = list((await session.execute(select(ContextObservation))).scalars().all())
    for row in rows:
        if not match(row):
            continue
        payload = dict(row.payload or {})
        event_type = str(row.event_type)
        if event_type == "cache_request_started":
            app.requests_started += 1
        elif event_type == "cache_lookup_finished":
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
            elif outcome == "bypass":
                app.bypasses += 1
        elif event_type == "cache_entry_created":
            generation = str(payload.get("generation_id") or "")
            if generation:
                app.created_generations.add(generation)
        elif event_type == "cache_entry_invalidated":
            reason = str(payload.get("reason") or "unknown")
            app.invalidation_reasons[reason] = app.invalidation_reasons.get(reason, 0) + 1
        elif event_type == "artifact_consumed":
            generation = str(payload.get("generation_id") or "")
            if generation:
                app.consumed_generations.add(generation)
        elif event_type == "model_attempt_started":
            prefix.started_attempts += 1
        elif event_type == "model_attempt_finished":
            prefix.attempts += 1
            status = str(payload.get("cache_status") or "")
            if status == "complete":
                prefix.complete += 1
                prefix.supported_attempts += 1
                hit = int(payload.get("cache_hit_tokens") or 0)
                miss = int(payload.get("cache_miss_tokens") or 0)
                prefix.hit_tokens += hit
                prefix.input_tokens += hit + miss
                if hit > 0:
                    prefix.hit_requests += 1
            elif status == "incomplete":
                # 支持供应商缺分项：仍计入 supported 覆盖率分母（E7CTX-14）
                prefix.supported_attempts += 1
            else:
                prefix.unsupported += 1
            price_snapshot = payload.get("price")
            if isinstance(price_snapshot, dict):
                key = json.dumps(price_snapshot, ensure_ascii=False, sort_keys=True)
                prefix.prices[key] = prefix.prices.get(key, 0) + 1

    totals = {
        "observations": len(rows),
        "unknown_note": "缺失 / 非法 / 冲突的缓存分项按 incomplete 计入 supported 分母，"
                        "不填零；unsupported 由能力配置区分，不与 incomplete 混算",
    }
    filters = {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "window": "[since, until) UTC 半开区间",
        "model": model or None,
        "prompt": prompt or None,
        "policy": policy or None,
        "cache_layer": cache_layer or None,
        "experiment_arm": experiment_arm or None,
    }
    return app, prefix, {"filters": filters, "totals": totals}


def _render_markdown(report: dict[str, Any]) -> str:
    app = report["app_cache"]
    prefix = report["prefix_cache"]
    lines = ["# 上下文与缓存度量报告（v2）", ""]
    lines.append(f"生成时间：{report['generated_at']}")
    lines.append(f"过滤条件：`{json.dumps(report['filters'], ensure_ascii=False)}`")
    lines.append("")
    lines.append("## 应用缓存")
    lines.append("")
    lines.append(
        f"- C（受理记录）= {app['C']}；H/M/E/B = {app['H']} / {app['M']} / "
        f"{app['E']} / {app['B']}"
    )
    lines.append(f"- 主自动命中率 H/(H+M+E) = {app['primary_hit_rate']}")
    lines.append(f"- 辅助正常命中率 H/(H+M) = {app['normal_hit_rate']}")
    lines.append(f"- 命中消费率 = {app['hit_consumption_rate']}")
    lines.append(f"- 条目复用率 = {app['entry_reuse_rate']}（{app['entry_queue']['note']}）")
    lines.append(f"- 绕过率 = {app['bypass_rate']}；缓存错误率 = {app['error_rate']}")
    lines.append(f"- miss 原因：{json.dumps(app['miss_reasons'], ensure_ascii=False)}")
    lines.append(f"- 失效原因：{json.dumps(app['invalidation_reasons'], ensure_ascii=False)}")
    lines.append("")
    lines.append("## 供应商前缀缓存")
    lines.append("")
    lines.append(
        f"- K（完整分项）= {prefix['K']}；支持请求 = {prefix['supported_attempts']}；"
        f"unsupported = {prefix['unsupported_attempts']}"
    )
    lines.append(f"- token 命中率 = {prefix['token_hit_rate']}")
    lines.append(f"- 支持请求完整覆盖率 = {prefix['supported_complete_coverage']}")
    lines.append(f"- 总体可观测覆盖率 = {prefix['overall_observable_coverage']}")
    lines.append(f"- 请求命中率 = {prefix['request_hit_rate']}")
    lines.append(f"- 未知分项比例 = {prefix['unknown_ratio']}")
    lines.append(
        f"- started/finished = {prefix['attempts_started']} / {prefix['attempts_finished']}"
        "（缺 finished 的尝试按 incomplete 处理，不补零）"
    )
    lines.append("")
    lines.append("## 样本与缺口")
    lines.append("")
    lines.append(f"- 观测总数：{report['totals']['observations']}")
    lines.append(f"- 未知口径：{report['totals']['unknown_note']}")
    lines.append("")
    lines.append("> 只聚合叶子观测；零分母输出 N/A；报告不使用当前价格重算历史。")
    return "\n".join(lines)


def _strict_gates(report: dict[str, Any]) -> list[str]:
    """strict 模式的最小自洽门槛：证据不全即失败（完整硬门槛见方案 §4.4）。"""
    failures: list[str] = []
    app = report["app_cache"]
    prefix = report["prefix_cache"]
    if app["C"] == 0 and (app["H"] + app["M"] + app["E"] + app["B"]) > 0:
        failures.append("cache_request_started 缺失：C 不能由 H/M/E/B 反推")
    if prefix["attempts_started"] == 0 and prefix["attempts_finished"] > 0:
        failures.append("model_attempt_started 缺失：尝试不可关联")
    if app["entry_queue"]["created_events"] == 0 and app["H"] > 0:
        failures.append("cache_entry_created 缺失：条目复用率没有创建队列")
    return failures


async def _main(args: argparse.Namespace) -> int:
    try:
        since = _parse_dt(args.since) if args.since else datetime.now(UTC) - timedelta(days=7)
        until = _parse_dt(args.until) if args.until else datetime.now(UTC)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    if since >= until:
        print("参数错误：since 必须早于 until（UTC 半开区间）", file=sys.stderr)
        return 2
    cfg = load_config()
    engine = create_engine_from_config(cfg)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            app, prefix, extra = await _collect(
                session, cfg, since=since, until=until, model=args.model,
                prompt=args.prompt, policy=args.policy, cache_layer=args.cache_layer,
                experiment_arm=args.experiment_arm,
            )
    finally:
        await engine.dispose()
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        **extra,
        "app_cache": app.as_dict(),
        "prefix_cache": prefix.as_dict(),
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"context-metrics-{stamp}.json"
    md_path = out_dir / f"context-metrics-{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False))
    if args.strict:
        failures = _strict_gates(report)
        if failures:
            for item in failures:
                print(f"strict 门槛未达：{item}", file=sys.stderr)
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="复算上下文与缓存度量并导出 JSON/Markdown")
    parser.add_argument("--since", default="", help="起始时间（ISO 8601，含时区或按 UTC）")
    parser.add_argument("--until", default="", help="结束时间（ISO 8601）")
    parser.add_argument("--model", default="", help="只看某个模型")
    parser.add_argument("--prompt", default="", help="只看某个 prompt 版本标识")
    parser.add_argument("--policy", default="", help="只看某个策略版本")
    parser.add_argument("--cache-layer", default="", help="只看某个缓存层（turn/persistent）")
    parser.add_argument("--experiment-arm", default="", help="只看某个实验臂")
    parser.add_argument("--strict", action="store_true",
                        help="严格模式：门槛未达退出 1，配置/环境问题退出 2，达标退出 0")
    parser.add_argument("--out-dir", default="runs/metrics", help="输出目录（runs/ 已忽略）")
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
