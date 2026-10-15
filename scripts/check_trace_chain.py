"""按轮次 / trace 读回调用链并核对口径（运维/验收脚本，S7.22）。

用途：给定一轮对话（`--turn-id`）或一条 trace（`--trace-id`），从库里取出 `trace_id`，
      经 Langfuse 公开 API（`/api/public/v2/observations`，游标分页 + 显式字段组）
      拉回整条 trace 的 observations，逐条核对：
      1. 根 span 存在（对话轮次是 `chat.turn`，API-only 链路是 `analyze`）；
      2. 工具 span（`tool.*`）挂在轮次根下；
      3. worker 侧 span（`analyze` / `worker.*` / `node.*`）挂在提交分析的工具 span 下
         （本轮没提交分析时跳过）；
      4. 叶子 generation 有用量 / 成本，或如实标"未知 / 失败"（不填零冒充准确）；
      5. trace 墙钟耗时与消息 `latency_ms` 同口径（默认容差 2000ms）。
输入：`--turn-id <uuid>` 或 `--trace-id <id>`（二选一）；`--tolerance-ms` 调容差；
      `--save <path>` 把云端原始 JSON 落盘（验收证据）；`--json` 输出机器可读结果。
输出：逐条核对结果 + span 树；退出码 0 = 全部通过，1 = 有失败 / 数据缺失，2 = 用法或配置错误。

口径（见《可观测.md》）：
- **只读**：库里查询 + Langfuse GET，不写任何状态。
- 云导出有延迟：404 时提示稍后再试，不把"还没收到"当成"没上报"。
- 耗时按边界：整轮 = 开始到收尾墙钟；父子不相加；与落库 `latency_ms` 用明确容差比对。
- 追踪关闭 / 轮次太早没有 `trace_id`：给出明确说明并以 1 退出（这不是失败，是"没有证据"）。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from xhs_agent.config import AppConfig, ConfigError, load_config
from xhs_agent.db.session import create_engine_from_config, create_session_factory

DEFAULT_TOLERANCE_MS = 2000
# 游标分页的安全上限（一条轮次 trace 的 observation 数量远小于 100 × 10）
MAX_PAGES = 10


@dataclass
class Check:
    """一条核对结果：`ok=None` 表示本轮没有对应的可核对项（跳过，不算失败）。"""

    name: str
    ok: bool | None
    detail: str


# ---------------------------------------------------------------------------
# 纯函数部分（可离线单测）
# ---------------------------------------------------------------------------


def observation_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or "")


def observation_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or "")


def observation_parent(item: dict[str, Any]) -> str:
    return str(item.get("parentObservationId") or item.get("parent_observation_id") or "")


def observation_type(item: dict[str, Any]) -> str:
    return str(item.get("type") or "").upper()


def parse_time(value: Any) -> dt.datetime | None:
    """解析 ISO 时间（兼容 `Z` 结尾）；解析不了返回 None。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def observation_latency_ms(item: dict[str, Any]) -> int | None:
    """单个 observation 的耗时（毫秒）：优先起止时间，退回云端 `latency`（秒）。"""
    start, end = parse_time(item.get("startTime")), parse_time(item.get("endTime"))
    if start is not None and end is not None and end >= start:
        return int((end - start).total_seconds() * 1000)
    latency = item.get("latency")
    try:
        value = float(latency)                       # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return int(value * 1000) if value >= 0 else None


def trace_wall_ms(observations: list[dict[str, Any]]) -> int | None:
    """整条 trace 的墙钟耗时：最早开始 → 最晚结束（父子不相加）。"""
    starts = [item for item in (parse_time(o.get("startTime")) for o in observations)
              if item is not None]
    ends = [item for item in (parse_time(o.get("endTime")) for o in observations)
            if item is not None]
    if not starts or not ends:
        return None
    return int((max(ends) - min(starts)).total_seconds() * 1000)


def _by_id(observations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {observation_id(item): item for item in observations if observation_id(item)}


def ancestor_names(item: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[str]:
    """从根到该 observation 的名字链（含自身）；断链或成环时提前停下。"""
    chain = [observation_name(item)]
    seen = {observation_id(item)}
    cursor = by_id.get(observation_parent(item))
    while cursor is not None and observation_id(cursor) not in seen:
        seen.add(observation_id(cursor))
        chain.append(observation_name(cursor))
        cursor = by_id.get(observation_parent(cursor))
    return list(reversed(chain))


def has_usage(item: dict[str, Any]) -> bool:
    for key in ("usageDetails", "usage"):
        value = item.get(key)
        if isinstance(value, dict) and value:
            return True
    return False


def has_cost(item: dict[str, Any]) -> bool:
    for key in ("costDetails", "calculatedTotalCost", "totalCost"):
        value = item.get(key)
        if isinstance(value, dict) and value:
            return True
        if isinstance(value, (int, float)) and value:
            return True
    return False


def is_unknown_usage(item: dict[str, Any]) -> bool:
    """失败或如实标"未知"的叶子：不算"没记用量"。"""
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    if str(metadata.get("usage_status") or "").lower() == "unknown":
        return True
    return str(item.get("level") or "").upper() == "ERROR"


def tree_lines(observations: list[dict[str, Any]]) -> list[str]:
    """把 observations 画成缩进树（根在前，子按开始时间排），供人工核对层级。"""
    by_id = _by_id(observations)
    children: dict[str, list[dict[str, Any]]] = {}
    for item in observations:
        parent = observation_parent(item)
        if parent and parent in by_id:
            children.setdefault(parent, []).append(item)

    def sort_key(item: dict[str, Any]) -> tuple[str, str]:
        return (str(item.get("startTime") or ""), observation_id(item))

    lines: list[str] = []

    def walk(item: dict[str, Any], depth: int) -> None:
        latency = observation_latency_ms(item)
        marks = []
        if observation_type(item) == "GENERATION":
            marks.append("有用量" if has_usage(item) else
                         ("用量未知" if is_unknown_usage(item) else "缺用量"))
        if str(item.get("level") or "").upper() == "ERROR":
            marks.append("错误")
        suffix = f"（{latency} ms）" if latency is not None else ""
        extra = f" [{'/'.join(marks)}]" if marks else ""
        lines.append(f"{'  ' * depth}- {observation_name(item)}{suffix}{extra}")
        for child in sorted(children.get(observation_id(item), []), key=sort_key):
            walk(child, depth + 1)

    roots = [item for item in observations if observation_parent(item) not in by_id]
    for root in sorted(roots, key=sort_key):
        walk(root, 0)
    return lines


def verify_trace(trace: dict[str, Any], *, expected_latency_ms: int | None = None,
                 tolerance_ms: int = DEFAULT_TOLERANCE_MS) -> list[Check]:
    """对一条云端 trace 跑全部核对口径；返回逐条结果（纯函数，可离线单测）。"""
    observations = [item for item in (trace.get("observations") or []) if isinstance(item, dict)]
    by_id = _by_id(observations)
    roots = [item for item in observations if observation_parent(item) not in by_id]
    root_names = "、".join(observation_name(item) for item in roots) or "（空 trace）"
    checks: list[Check] = []

    # 1. 入口 span：优先对话轮次（chat.turn）；API 直连链路的入口是 analyze。
    #    注意**不能按返回顺序取第一个**——云端列表顺序不保证（实测 analyze 会排在 chat.turn 前）
    entry = next((item for item in observations if observation_name(item) == "chat.turn"), None)
    style = "对话轮次"
    if entry is None:
        entry = next((item for item in observations if observation_name(item) == "analyze"), None)
        style = "API 分析链路"
    checks.append(Check(
        "入口 span", entry is not None,
        f"入口 = {style}（{observation_name(entry)}）；物理根 = {root_names}；"
        f"共 {len(observations)} 个 observation" if entry is not None
        else f"没找到 chat.turn / analyze；物理根 = {root_names}"))

    # 2. 工具 span 挂在轮次根下（对话轮次）
    tools = [item for item in observations if observation_name(item).startswith("tool.")]
    if not tools:
        checks.append(Check("工具 span", None, "本轮无工具 span（纯回复轮次）"))
    else:
        bad = [observation_name(item) for item in tools
               if entry is None or observation_name(entry) not in ancestor_names(item, by_id)]
        checks.append(Check(
            "工具 span", not bad,
            f"{len(tools)} 个：{'、'.join(observation_name(t) for t in tools)}"
            + (f"；断链：{'、'.join(bad)}" if bad else "")))

    # 3. worker / 分析节点：对话轮次下必须挂在提交分析的工具 span 下（设计约定 §3）
    workers = [item for item in observations
               if observation_name(item).startswith(("worker.", "node."))
               or (observation_name(item) == "analyze" and entry is not None
                   and observation_id(item) != observation_id(entry))]
    if not workers:
        checks.append(Check("worker 链", None, "本轮没有 worker / 节点 span（未提交分析）"))
    elif style != "对话轮次":
        checks.append(Check("worker 链", None,
                            f"API 风格 trace（入口 = analyze）：{len(workers)} 个 worker / 节点 span"
                            "不在本脚本硬核对范围，层级请看下面的 span 树"))
    else:
        bad = []
        for item in workers:
            chain = ancestor_names(item, by_id)
            if not any(name.startswith("tool.") for name in chain):
                bad.append(f"{observation_name(item)}（链：{' → '.join(chain)}）")
        checks.append(Check(
            "worker 链", not bad,
            f"{len(workers)} 个 worker / 节点 span" + (f"；未挂工具 span：{bad}" if bad else "")))

    # 4. 叶子 generation 的用量 / 成本
    generations = [item for item in observations if observation_type(item) == "GENERATION"]
    if not generations:
        checks.append(Check("叶子用量", None, "trace 里没有 generation"))
    else:
        missing = [observation_name(item) for item in generations
                   if not has_usage(item) and not is_unknown_usage(item)]
        no_cost = [observation_name(item) for item in generations
                   if has_usage(item) and not has_cost(item)]
        detail = (f"{len(generations)} 个 generation；"
                  f"{sum(1 for item in generations if has_usage(item))} 个有用量")
        if missing:
            detail += f"；缺用量且未标未知：{'、'.join(missing)}"
        if no_cost:
            detail += f"；有用量但缺成本：{'、'.join(no_cost)}"
        checks.append(Check("叶子用量", not missing, detail))

    # 5. 墙钟耗时与落库 latency_ms 同口径
    wall = trace_wall_ms(observations)
    if expected_latency_ms is None or wall is None:
        checks.append(Check("耗时口径", None,
                            "缺少一边的耗时（库里的 latency_ms 或云端起止时间）"))
    else:
        diff = abs(wall - expected_latency_ms)
        checks.append(Check(
            "耗时口径", diff <= tolerance_ms,
            f"云端墙钟 {wall} ms vs 落库 {expected_latency_ms} ms（差 {diff} ms，"
            f"容差 {tolerance_ms} ms）"))
    return checks


# ---------------------------------------------------------------------------
# 云端与库（薄封装，不进单测）
# ---------------------------------------------------------------------------


def load_trace(cfg: AppConfig, trace_id: str) -> dict[str, Any] | None:
    """拉一条 trace 的 observations，组成与旧版 trace 详情同形的 dict；查不到返回 None。

    用新组织唯一可用的 `GET /api/public/v2/observations`（旧版 `/api/public/traces/{id}` 对
    2026-09-16 之后创建的组织返回 410），游标分页取全；`fields` 显式加 `usage,cost,metadata`
    ——默认字段组里没有 `usageDetails` / `costDetails`，叶子用量就核不了。
    """
    public, secret, host = cfg.langfuse_keys
    url = f"{host.rstrip('/')}/api/public/v2/observations"
    params: dict[str, Any] = {"traceId": trace_id, "limit": 100,
                              "fields": "core,basic,usage,cost,metadata"}
    observations: list[dict[str, Any]] = []
    cursor = ""
    for _page in range(MAX_PAGES):
        query = dict(params, cursor=cursor) if cursor else dict(params)
        response = httpx.get(url, params=query, auth=(public, secret), timeout=30.0)
        if response.status_code in (404, 410):
            return None
        if response.status_code == 429:
            raise RuntimeError("云端限流（429）：稍等一会儿再跑一次")
        response.raise_for_status()
        body = response.json()
        data = [item for item in (body.get("data") or []) if isinstance(item, dict)]
        observations.extend(data)
        cursor = str((body.get("meta") or {}).get("cursor") or "")
        if not data or not cursor:
            break
    if not observations:
        return None
    return {"id": trace_id, "observations": observations}


async def _lookup_turn(cfg: AppConfig, turn_id: str) -> dict[str, Any] | None:
    engine = create_engine_from_config(cfg)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            row = (await session.execute(text(
                "select trace_id, status, latency_ms, progress from chat_messages "
                "where turn_id = :turn_id order by created_at desc limit 1"),
                {"turn_id": turn_id})).mappings().first()
            return dict(row) if row else None
    finally:
        await engine.dispose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按轮次 / trace 读回并核对调用链")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--turn-id", help="对话轮次 id（从 chat_messages.trace_id 取 trace）")
    group.add_argument("--trace-id", help="直接指定 trace id")
    parser.add_argument("--tolerance-ms", type=int, default=DEFAULT_TOLERANCE_MS,
                        help=f"墙钟耗时容差（默认 {DEFAULT_TOLERANCE_MS}）")
    parser.add_argument("--save", help="把云端原始 JSON 落到该路径（验收证据）")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="输出机器可读结果")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2

    expected_latency_ms: int | None = None
    trace_id = args.trace_id or ""
    if args.turn_id:
        row = asyncio.run(_lookup_turn(cfg, args.turn_id))
        if row is None:
            print(f"[未找到] 没有 turn_id = {args.turn_id} 的消息")
            return 2
        trace_id = str(row.get("trace_id") or "")
        expected_latency_ms = int(row["latency_ms"]) if row.get("latency_ms") else None
        if not trace_id:
            print(f"[没有证据] 该轮次 status = {row.get('status')}，但没关联 trace_id"
                  "（追踪关闭或轮次早于 S7.21，属预期情况）")
            return 1

    public, secret, host = cfg.langfuse_keys
    if not (public and secret and host):
        print("[配置错误] LANGFUSE 三件套不齐，没法读回云端 trace")
        return 2

    try:
        trace = load_trace(cfg, trace_id)
    except RuntimeError as exc:                  # 429 等云端限流：给出可照做的提示
        print(f"[稍后重试] {exc}")
        return 1
    if trace is None:
        print(f"[还没收到] 云端没有 trace {trace_id}——导出有延迟，稍后重试")
        return 1

    if args.save:
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save).write_text(json.dumps(trace, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"[已存证] {args.save}")

    checks = verify_trace(trace, expected_latency_ms=expected_latency_ms,
                          tolerance_ms=args.tolerance_ms)
    observations = [item for item in (trace.get("observations") or []) if isinstance(item, dict)]

    if args.as_json:
        print(json.dumps({"trace_id": trace_id,
                          "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail}
                                     for c in checks]}, ensure_ascii=False, indent=2))
    else:
        print(f"trace {trace_id}（{len(observations)} 个 observation）\n")
        for check in checks:
            mark = "通过" if check.ok else ("跳过" if check.ok is None else "失败")
            print(f"[{mark}] {check.name}：{check.detail}")
        print("\nspan 树（按开始时间）：")
        for line in tree_lines(observations):
            print(f"  {line}")

    failed = [c for c in checks if c.ok is False]
    if failed:
        print(f"\n有 {len(failed)} 项没通过；逐条看上面对应的 detail。")
        return 1
    print("\n核对完成：硬口径全部通过（跳过项 = 本轮没有对应环节）。")
    return 0


if __name__ == "__main__":                            # pragma: no cover - 入口
    sys.exit(main())
