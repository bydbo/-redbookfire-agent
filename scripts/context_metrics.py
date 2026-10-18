"""上下文与缓存度量复算脚本（v3，A09）。

用途：从 `context_observations`（本地事实源）复算应用缓存与供应商前缀缓存指标，输出 JSON 与
Markdown；保留原始分子 / 分母、过滤条件、未知数与**每次尝试的价格快照**，缺失不填零、
零分母输出 N/A。

口径（二次验收 A09）：

- **先按 event_id 去重**（重复投递只算一次），再按 `会话 + cache_request_id`
  （实现上是 `session_id + turn_id + call_id`）关联受理、最终结果与实际消费；
- **每个请求只有一个主结果**：取该请求最后一次 `cache_lookup_finished`；没有结果的请求计入
  `incomplete`，不能从已完成结果反推分母；
- **C 来自受理**（`cache_request_started`），不由 H/M/E/B 反推；
- **命中消费率 = 有真实消费的命中请求数 / H**（按请求，不按 generation——同一 generation
  三次命中各消费一次就是 3/3）；
- **条目复用率 = 创建队列中被再次消费的不同 generation / 创建队列 generation 数**；
  只认 `used_for=image_clue` 的真实缓存消费，首次 miss 与普通选定版本不算缓存命中；
- **窗口**：UTC 半开 `[since, until)`。请求按**受理时间**归队，关联到观察截止前的终态与
  消费；条目队列按**创建时间**归队，关联创建后至观察截止前的消费（不要求消费也在窗口内）；
- **前缀尝试**按 `logical_call_id + attempt` 关联 started 与终态：started 已固化能力，
  缺终态仍进入**全部尝试**覆盖率分母（记为 incomplete / unknown），成功响应覆盖单独报告；
- **strict**：正常观测完整率 ≥ 99%、支持供应商成功响应完整分项覆盖 ≥ 95%，并检查缺关联、
  非法分项、分母错误与适用样本是否为空；空报告不能通过；有能力证据的 N/A 单列但不据此达标。
- 退出码：全部适用门槛达标 0；门槛未达 / 必要证据缺失 1；参数、环境、配置无法报告 2。
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

#: strict 门槛（A09 第 6 条）
NORMAL_COMPLETENESS_MIN = 0.99
SUPPORTED_SUCCESS_COVERAGE_MIN = 0.95


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


@dataclass(frozen=True)
class Observation:
    """一条观测事实（脱离 ORM，便于离线复算与单测）。"""

    event_id: str
    session_id: str
    event_type: str
    created_at: datetime
    turn_id: str = ""
    call_id: str = ""
    attempt: int | None = None
    model: str = ""
    prompt_version: str = ""
    policy_version: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def request_key(self) -> tuple[str, str, str]:
        return (self.session_id, self.turn_id, self.call_id)


@dataclass
class RequestState:
    """一个逻辑缓存请求的受理与结果（A09：请求级、单一主结果）。"""

    key: tuple[str, str, str]
    started_at: datetime
    outcomes: list[tuple[datetime, str, str, str]] = field(default_factory=list)
    consumed_at: list[datetime] = field(default_factory=list)

    @property
    def primary(self) -> tuple[datetime, str, str, str] | None:
        if not self.outcomes:
            return None
        return sorted(self.outcomes, key=lambda item: item[0])[-1]

    @property
    def outcome(self) -> str:
        primary = self.primary
        return "" if primary is None else primary[1]


@dataclass
class AppCacheMetrics:
    requests: dict[tuple[str, str, str], RequestState] = field(default_factory=dict)
    orphan_lookups: int = 0
    duplicate_events: int = 0
    created: dict[str, datetime] = field(default_factory=dict)      # generation -> created_at
    consumed: dict[str, list[datetime]] = field(default_factory=dict)  # generation -> times
    miss_reasons: dict[str, int] = field(default_factory=dict)
    invalidation_reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        outcomes = {"hit": 0, "miss": 0, "error": 0, "bypass": 0}
        turn_hits = 0
        persistent_hits = 0
        hit_requests = 0
        consumed_hit_requests = 0
        for request in self.requests.values():
            primary = request.primary
            if primary is None:
                continue
            outcome, level = primary[1], primary[2]
            if outcome in outcomes:
                outcomes[outcome] += 1
            if outcome == "hit":
                hit_requests += 1
                if level == "turn":
                    turn_hits += 1
                else:
                    persistent_hits += 1
                if request.consumed_at:
                    consumed_hit_requests += 1
        c = len(self.requests)
        complete = sum(outcomes.values())
        # 条目复用：创建队列内的 generation 在创建后、观察截止前被真实消费
        reused = sum(
            1 for generation, created_at in self.created.items()
            if any(moment >= created_at for moment in self.consumed.get(generation, []))
        )
        return {
            "C": c,
            "C_source": "cache_request_started",
            "H": outcomes["hit"],
            "M": outcomes["miss"],
            "E": outcomes["error"],
            "B": outcomes["bypass"],
            "incomplete_requests": max(0, c - complete),
            "orphan_lookups": self.orphan_lookups,
            "duplicate_events_ignored": self.duplicate_events,
            "layer": {"turn": turn_hits, "persistent": persistent_hits},
            "primary_hit_rate": _rate(outcomes["hit"], complete),
            "normal_hit_rate": _rate(outcomes["hit"], outcomes["hit"] + outcomes["miss"]),
            "hit_consumption_rate": _rate(consumed_hit_requests, hit_requests),
            "hit_consumption_numerator": consumed_hit_requests,
            "hit_consumption_denominator": hit_requests,
            "entry_reuse_rate": _rate(reused, len(self.created)),
            "entry_queue": {
                "created_generations": len(self.created),
                "consumed_generations": len(self.consumed),
                "reused_in_queue": reused,
                "note": "分子只算创建后、观察截止前被真实消费（used_for=image_clue）的"
                        " generation；首次 miss / 普通选定版本不进入缓存命中",
            },
            "bypass_rate": _rate(outcomes["bypass"], c),
            "error_rate": _rate(outcomes["error"], complete),
            "normal_observation_completeness": _rate(complete, c),
            "miss_reasons": dict(sorted(self.miss_reasons.items())),
            "invalidation_reasons": dict(sorted(self.invalidation_reasons.items())),
        }


@dataclass
class PrefixMetrics:
    started: dict[str, datetime] = field(default_factory=dict)   # attempt key -> started_at
    finished: dict[str, dict[str, Any]] = field(default_factory=dict)
    orphan_finished: int = 0
    prices: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        attempts = len(self.started)
        finished = len(self.finished)
        without_terminal = max(0, attempts - finished)
        complete = 0
        unsupported = 0
        successes = 0
        success_supported = 0
        success_complete = 0
        input_tokens = 0
        hit_tokens = 0
        hit_requests = 0
        invalid_breakdown = 0
        for key in self.finished:
            payload = self.finished[key]
            status = str(payload.get("cache_status") or "")
            success = str(payload.get("status") or "") == "succeeded"
            if success:
                successes += 1
            if status == "unsupported":
                unsupported += 1
                continue
            if success:
                # 支持供应商的成功响应 = 完整分项 + 缺分项（后者进未知，不排除分母）
                success_supported += 1
            if status == "complete":
                hit = int(payload.get("cache_hit_tokens") or 0)
                miss = int(payload.get("cache_miss_tokens") or 0)
                if hit < 0 or miss < 0 or (hit + miss) <= 0:
                    invalid_breakdown += 1
                else:
                    complete += 1
                    input_tokens += hit + miss
                    hit_tokens += hit
                    if hit > 0:
                        hit_requests += 1
                    if success:
                        success_complete += 1
            price_snapshot = payload.get("price")
            if isinstance(price_snapshot, dict):
                price_key = json.dumps(price_snapshot, ensure_ascii=False, sort_keys=True)
                self.prices[price_key] = self.prices.get(price_key, 0) + 1
        supported = max(0, attempts - unsupported)
        unknown = max(0, supported - complete)
        return {
            "K": complete,
            "attempts_started": attempts,
            "attempts_finished": finished,
            "attempts_without_terminal": without_terminal,
            "incomplete_attempts": unknown,
            "supported_attempts": supported,
            "unsupported_attempts": unsupported,
            "orphan_finished": self.orphan_finished,
            "invalid_breakdown": invalid_breakdown,
            "input_tokens": input_tokens,
            "hit_tokens": hit_tokens,
            "token_hit_rate": _rate(hit_tokens, input_tokens),
            "supported_complete_coverage": _rate(complete, supported),
            "all_attempt_coverage": _rate(complete, attempts),
            "supported_success_coverage": _rate(success_complete, success_supported),
            "success_responses": successes,
            "unknown_ratio": _rate(unknown, supported),
            "request_hit_rate": _rate(hit_requests, complete),
            "price_snapshots": self.prices,
            "note": "支持供应商缺分项仍计入 supported 分母；缺终态按 incomplete/unknown 计入"
                    "全部尝试分母，不补零；成功响应覆盖与全部尝试覆盖分别报告",
        }


def _match_filters(
    row: Observation,
    *,
    model: str,
    prompt: str,
    policy: str,
    cache_layer: str,
    experiment_arm: str,
) -> bool:
    """过滤字段在受理 / 创建时固化；未知标识不因不一致被筛掉（只按显式过滤）。"""
    if model and row.model != model:
        return False
    if prompt and row.prompt_version != prompt:
        return False
    if policy and row.policy_version != policy:
        return False
    if cache_layer and str(row.payload.get("cache_layer") or "") != cache_layer:
        return False
    return not (experiment_arm and str(row.payload.get("experiment_arm") or "") != experiment_arm)


def analyze(
    rows: list[Observation],
    *,
    since: datetime,
    until: datetime,
    model: str = "",
    prompt: str = "",
    policy: str = "",
    cache_layer: str = "",
    experiment_arm: str = "",
) -> tuple[AppCacheMetrics, PrefixMetrics]:
    """固定事件的纯复算（A09）：去重 → 按请求 / 尝试关联 → 分子分母。

    请求按受理时间归队；查库结果与消费可以发生在受理事后（距观察截止前），但不能把
    窗口外的请求拉进来。条目队列按创建时间归队，消费只需在创建后、观察截止前。
    """
    app = AppCacheMetrics()
    prefix = PrefixMetrics()
    seen: set[str] = set()

    for row in sorted(rows, key=lambda item: item.created_at):
        if row.event_id in seen:
            app.duplicate_events += 1
            continue
        seen.add(row.event_id)
        if not _match_filters(row, model=model, prompt=prompt, policy=policy,
                              cache_layer=cache_layer, experiment_arm=experiment_arm):
            continue
        event_type = row.event_type
        payload = dict(row.payload or {})

        if event_type == "cache_request_started":
            if not (since <= row.created_at < until):
                continue
            request = app.requests.get(row.request_key)
            if request is None:
                app.requests[row.request_key] = RequestState(
                    key=row.request_key, started_at=row.created_at
                )
            continue

        if event_type == "cache_lookup_finished":
            if row.created_at >= until:
                continue
            request = app.requests.get(row.request_key)
            if request is None:
                app.orphan_lookups += 1
                continue
            outcome = str(payload.get("outcome") or "")
            reason = str(payload.get("reason") or "unknown")
            request.outcomes.append(
                (row.created_at, outcome, str(payload.get("level") or ""), reason)
            )
            if outcome == "miss":
                app.miss_reasons[reason] = app.miss_reasons.get(reason, 0) + 1
            continue

        if event_type == "cache_entry_created":
            generation = str(payload.get("generation_id") or "")
            if generation and since <= row.created_at < until:
                app.created.setdefault(generation, row.created_at)
            continue

        if event_type == "cache_entry_invalidated":
            reason = str(payload.get("reason") or "unknown")
            app.invalidation_reasons[reason] = app.invalidation_reasons.get(reason, 0) + 1
            continue

        if event_type == "artifact_consumed":
            generation = str(payload.get("generation_id") or "")
            used_for = str(payload.get("used_for") or "")
            # 只认真实缓存消费；选定编辑版（used_for=selected_clue）不算缓存命中
            if not generation or used_for != "image_clue":
                continue
            if row.created_at >= until:
                continue
            app.consumed.setdefault(generation, []).append(row.created_at)
            request = app.requests.get(row.request_key)
            if request is not None:
                request.consumed_at.append(row.created_at)
            continue

    # ---- 前缀尝试：第二遍按 logical_call_id + attempt 关联 ----
    for row in sorted(rows, key=lambda item: item.created_at):
        if row.event_type not in ("model_attempt_started", "model_attempt_finished"):
            continue
        if not _match_filters(row, model=model, prompt=prompt, policy=policy,
                              cache_layer=cache_layer, experiment_arm=experiment_arm):
            continue
        payload = dict(row.payload or {})
        identity = str(payload.get("identity") or "")
        if not identity or identity == "aggregate":
            continue
        attempt = int(row.attempt or payload.get("attempt") or 1)
        key = f"{row.session_id}:{identity}:{attempt}"
        if row.event_type == "model_attempt_started":
            if since <= row.created_at < until:
                prefix.started.setdefault(key, row.created_at)
        else:
            if row.created_at >= until:
                continue
            if key not in prefix.started:
                prefix.orphan_finished += 1
            prefix.finished[key] = payload
    return app, prefix


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
    del cfg  # 过滤与复算不依赖运行配置（价格取事件里的快照）
    orm_rows = list((await session.execute(select(ContextObservation))).scalars().all())
    rows = [
        Observation(
            event_id=str(row.event_id),
            session_id=str(row.session_id),
            event_type=str(row.event_type),
            created_at=row.created_at if row.created_at.tzinfo
            else row.created_at.replace(tzinfo=UTC),
            turn_id=str(row.turn_id or ""),
            call_id=str(row.call_id or ""),
            attempt=row.attempt,
            model=str(row.model or ""),
            prompt_version=str(row.prompt_version or ""),
            policy_version=str(row.policy_version or ""),
            payload=dict(row.payload or {}),
        )
        for row in orm_rows
    ]
    app, prefix = analyze(
        rows, since=since, until=until, model=model, prompt=prompt, policy=policy,
        cache_layer=cache_layer, experiment_arm=experiment_arm,
    )
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
    totals = {
        "observations": len(rows),
        "unknown_note": "缺终态 / 缺分项按 incomplete 计入 supported 分母，不填零；"
                        "unsupported 由明确能力证据区分",
    }
    return app, prefix, {"filters": filters, "totals": totals}


def _render_markdown(report: dict[str, Any]) -> str:
    app = report["app_cache"]
    prefix = report["prefix_cache"]
    lines = ["# 上下文与缓存度量报告（v3 / A09）", ""]
    lines.append(f"生成时间：{report['generated_at']}")
    lines.append(f"过滤条件：`{json.dumps(report['filters'], ensure_ascii=False)}`")
    lines.append("")
    lines.append("## 应用缓存")
    lines.append("")
    lines.append(
        f"- C（受理请求）= {app['C']}；H/M/E/B = {app['H']} / {app['M']} / "
        f"{app['E']} / {app['B']}；未完成 = {app['incomplete_requests']}"
    )
    lines.append(f"- 主自动命中率 H/(H+M+E+B) = {app['primary_hit_rate']}")
    lines.append(f"- 辅助正常命中率 H/(H+M) = {app['normal_hit_rate']}")
    lines.append(
        f"- 命中消费率（请求级）= {app['hit_consumption_rate']}"
        f"（{app['hit_consumption_numerator']}/{app['hit_consumption_denominator']}）"
    )
    lines.append(f"- 条目复用率 = {app['entry_reuse_rate']}（{app['entry_queue']['note']}）")
    lines.append(f"- 绕过率 = {app['bypass_rate']}；缓存错误率 = {app['error_rate']}")
    lines.append(f"- 正常观测完整率 = {app['normal_observation_completeness']}")
    lines.append(f"- miss 原因：{json.dumps(app['miss_reasons'], ensure_ascii=False)}")
    lines.append(f"- 失效原因：{json.dumps(app['invalidation_reasons'], ensure_ascii=False)}")
    lines.append("")
    lines.append("## 供应商前缀缓存")
    lines.append("")
    lines.append(
        f"- K（完整分项）= {prefix['K']}；支持尝试 = {prefix['supported_attempts']}；"
        f"unsupported = {prefix['unsupported_attempts']}"
    )
    lines.append(f"- token 命中率 = {prefix['token_hit_rate']}")
    lines.append(f"- 支持尝试完整覆盖率 = {prefix['supported_complete_coverage']}")
    lines.append(f"- 全部尝试覆盖率 = {prefix['all_attempt_coverage']}")
    lines.append(f"- 支持供应商成功响应覆盖 = {prefix['supported_success_coverage']}")
    lines.append(f"- 请求命中率 = {prefix['request_hit_rate']}")
    lines.append(f"- 未知分项比例 = {prefix['unknown_ratio']}")
    lines.append(
        f"- started/finished = {prefix['attempts_started']} / {prefix['attempts_finished']}"
        f"；缺终态 = {prefix['attempts_without_terminal']}；缺关联 = {prefix['orphan_finished']}"
        "（缺终态按 incomplete 处理，不补零）"
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
    """strict 门槛（A09 第 6 条）：门槛未达或证据不全即失败；空报告不能通过。"""
    failures: list[str] = []
    app = report["app_cache"]
    prefix = report["prefix_cache"]
    if app["C"] == 0:
        failures.append("没有受理请求（C=0）：空报告不能通过")
    completeness = app["normal_observation_completeness"]
    if completeness == "N/A" or float(completeness) < NORMAL_COMPLETENESS_MIN:
        failures.append(
            f"正常观测完整率 {completeness} 低于 {NORMAL_COMPLETENESS_MIN:.0%}"
        )
    if app["incomplete_requests"]:
        failures.append(f"有 {app['incomplete_requests']} 个受理请求缺少最终结果")
    if app["orphan_lookups"]:
        failures.append(f"有 {app['orphan_lookups']} 条查询结果缺少受理记录")
    if prefix["attempts_started"] == 0:
        failures.append("没有前缀尝试样本（attempts_started=0）")
    if prefix["success_responses"] == 0:
        failures.append("没有成功响应样本，成功覆盖不可判定")
    if prefix["orphan_finished"]:
        failures.append(f"有 {prefix['orphan_finished']} 个终态缺少 started 关联")
    if prefix["invalid_breakdown"]:
        failures.append(f"有 {prefix['invalid_breakdown']} 个非法分项（input≠hit+miss）")
    coverage = prefix["supported_success_coverage"]
    if coverage == "N/A" or float(coverage) < SUPPORTED_SUCCESS_COVERAGE_MIN:
        failures.append(
            f"支持供应商成功响应覆盖 {coverage} 低于 {SUPPORTED_SUCCESS_COVERAGE_MIN:.0%}"
        )
    if prefix["attempts_without_terminal"]:
        failures.append(
            f"有 {prefix['attempts_without_terminal']} 个支持尝试缺少终态（按 incomplete 计）"
        )
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
