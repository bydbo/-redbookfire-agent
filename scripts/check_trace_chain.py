"""按轮次 / trace 读回调用链并核对口径（运维/验收脚本，S7.22；R6 / OBS-08 全面加固）。

用途：给定一轮对话（`--turn-id`）或一条 trace（`--trace-id`），从库里取出 `trace_id`，
      经 Langfuse 公开 API（`/api/public/v2/observations`，游标分页 + 显式字段组）
      拉回整条 trace 的 observations，逐条核对：
      1. 入口 span 唯一（对话轮次是 `chat.turn`，API-only 链路是 `analyze`；
         出现多个 `chat.turn` 视为串轮，直接判失败）；
      2. 引用完整：任何 observation 的 parent 都必须在本 trace 内（孤儿引用 = 链路残缺）；
      3. 工具 span（`tool.*`）挂在轮次根下（`--expect tool` 时"没有"也算失败）；
      4. worker 侧 span（`analyze` / `worker.*` / `node.*`）挂在提交分析的工具 span 下
         （`--expect workers` 时"没有"也算失败）；
      5. 视觉调用：`--expect vision` 要求出现 `image_hotspot_clue(_multi)` generation，
         `--expect no-vision` 要求不出现（一次多图调用只有一个逻辑 generation）；
      6. 叶子 generation 有**有效**用量与成本，或 metadata 如实标 `usage_status=unknown`
         （标未知的算"待验证"，不算失败；ERROR 级别**不再**自动等于未知）；
      7. 账本核对：generation 用量 / 成本求和必须与落库
         `prompt_tokens` / `completion_tokens` / `cost_cny` 一致（token 精确、成本 ±0.01）；
         失败 / 取消轮次或入口 metadata 标了未知调用时为"待验证"；
      8. 耗时口径：**入口 span 的耗时**（不是 trace 墙钟——并行 worker 会拉宽墙钟）
         与消息 `latency_ms` 同口径（默认容差 2000ms）。
输入：`--turn-id <uuid>` 或 `--trace-id <id>`（二选一）；`--expect tool,vision,...` 声明
      本轮应有的环节；`--tolerance-ms` 调容差；`--retries` 云端 404/429 重试次数；
      `--save <path>` 把云端原始 JSON 落盘（验收证据）；`--json` 输出机器可读结果。
输出：逐条核对结果 + span 树；退出码 0 = 全部通过（无失败、无待验证），
      1 = 有失败，2 = 用法或配置错误，3 = 仅待验证（有 unknown 用量 / 缺账本一边，
      不能视为完整通过，需人工确认后重跑）。

口径（见《可观测.md》）：
- **只读**：库里查询 + Langfuse GET，不写任何状态。
- 云导出有延迟：404 / 429 按 `--retries` 有界重试（退避 5s × 次数），仍取不到提示稍后再试，
  不把"还没收到"当成"没上报"。
- 耗时按边界：整轮看入口 span；父子不相加；与落库 `latency_ms` 用明确容差比对。
- 追踪关闭 / 轮次太早没有 `trace_id`：给出明确说明并以 1 退出（这不是失败，是"没有证据"）。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from xhs_agent.config import AppConfig, ConfigError, load_config
from xhs_agent.db.session import create_engine_from_config, create_session_factory

DEFAULT_TOLERANCE_MS = 2000
DEFAULT_RETRIES = 2
# 游标分页的安全上限（一条轮次 trace 的 observation 数量远小于 100 × 10）
MAX_PAGES = 10
# 视觉等价 generation 的名字（R3 / OBS-06：task_id 即 generation 名，多图也是同一个）
VISION_GENERATIONS = ("image_hotspot_clue", "image_hotspot_clue_multi")
# --expect 可声明的环节（逗号分隔多选）
EXPECT_CHOICES = ("tool", "vision", "no-vision", "workers")
# 账本成本核对容差（元）：落库是 Decimal(10,4)，云端有分位舍入
LEDGER_COST_TOLERANCE_CNY = 0.01


@dataclass
class Check:
    """一条核对结果。

    `ok=None` 且 `pending=False`：本轮没有对应的可核对项（不适用，不算失败）；
    `ok=None` 且 `pending=True`：缺证据 / 待验证（不算失败，但也**不能视为通过**，
    整个脚本将以退出码 3 结束，提示人工确认）。
    """

    name: str
    ok: bool | None
    detail: str
    pending: bool = False


# ---------------------------------------------------------------------------
# 纯函数部分（可离线单测）
# ---------------------------------------------------------------------------


def observation_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or "")


def observation_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or "")


def observation_parent(item: dict[str, Any]) -> str:
    return str(
        item.get("parentObservationId") or item.get("parent_observation_id") or ""
    )


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
        value = float(latency)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return int(value * 1000) if value >= 0 else None


def trace_wall_ms(observations: list[dict[str, Any]]) -> int | None:
    """整条 trace 的墙钟跨度：最早开始 → 最晚结束（只作展示参考，不用于耗时核对）。"""
    starts = [
        item
        for item in (parse_time(o.get("startTime")) for o in observations)
        if item is not None
    ]
    ends = [
        item
        for item in (parse_time(o.get("endTime")) for o in observations)
        if item is not None
    ]
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


def usage_values_valid(item: dict[str, Any]) -> bool:
    """用量字典存在且每个值都是**非负数值**（bool 不算数值；0 是合法值）。

    防"填零冒充"的反面：0 用量 + 0 成本对免费 / 缓存场景是合法的，但 -1、
    `"100"`（字符串）这类脏值说明上报口径有问题，必须暴露。
    """
    for key in ("usageDetails", "usage"):
        value = item.get(key)
        if isinstance(value, dict) and value:
            for number in value.values():
                if (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or number < 0
                ):
                    return False
            return True
    return False


def has_cost(item: dict[str, Any]) -> bool:
    """有成本记录：字典非空，或数值（**显式 0 也算**——免费调用如实记 0，不算缺）。"""
    for key in ("costDetails", "calculatedTotalCost", "totalCost"):
        value = item.get(key)
        if isinstance(value, dict):
            if value:
                return True
        elif (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
        ):
            return True
    return False


def usage_status_of(item: dict[str, Any]) -> str:
    """metadata.usage_status 的小写值（actual / estimated / unknown）；缺失返回空串。

    注意：ERROR 级别**不再**自动等于"未知"（R6 修正）——错误叶子的用量状态只看
    metadata 如实上报的字段，避免把"失败但已知用量"误判成未知。
    """
    raw = item.get("metadata")
    metadata = raw if isinstance(raw, dict) else {}
    return str(metadata.get("usage_status") or "").lower()


def is_vision_generation(item: dict[str, Any]) -> bool:
    return observation_name(item) in VISION_GENERATIONS


def generation_tokens(item: dict[str, Any]) -> tuple[int, int]:
    """generation 的 (prompt, completion) token 数；缺了按 0 计（叶子用量口径另行把关）。"""
    for key in ("usageDetails", "usage"):
        value = item.get(key)
        if isinstance(value, dict) and value:
            prompt = value.get("input", value.get("prompt", 0))
            completion = value.get("output", value.get("completion", 0))
            try:
                return int(prompt or 0), int(completion or 0)
            except (TypeError, ValueError):
                return 0, 0
    return 0, 0


def generation_cost(item: dict[str, Any]) -> float:
    """generation 的成本（元）：costDetails.total 优先，退回 calculatedTotalCost / totalCost。"""
    for key in ("costDetails",):
        value = item.get(key)
        if isinstance(value, dict):
            for sub in ("total", "totalCost"):
                if sub in value:
                    try:
                        return float(value[sub])
                    except (TypeError, ValueError):
                        return 0.0
    for key in ("calculatedTotalCost", "totalCost"):
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


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
            status = usage_status_of(item)
            if status == "unknown":
                marks.append("用量未知")
            elif has_usage(item):
                marks.append("有用量" if status != "estimated" else "估算用量")
            else:
                marks.append("缺用量")
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


def _ledger_check(
    generations: list[dict[str, Any]],
    ledger: dict[str, Any] | None,
    *,
    unknown_calls: int,
    turn_status: str,
) -> Check:
    """账本核对（R6 / OBS-08 / OBS-10）：云端 generation 求和 vs 落库三件套。

    - 没有落库账本（`--trace-id` 直连 / 旧轮次）：不适用（跳过）；
    - 失败 / 取消轮次，或入口 metadata 标了 `usage_unknown_calls > 0`：**待验证**
      （失败轮只保证已知部分入账，精确核对本来就轮不到它）；
    - 其余：token 精确相等、成本 ±0.01，差一点都算失败。
    """
    if ledger is None:
        return Check("账本核对", None, "没有落库账本（--trace-id 直连或旧轮次），跳过")
    if turn_status and turn_status != "succeeded":
        return Check(
            "账本核对",
            None,
            f"轮次状态 = {turn_status}：失败 / 取消轮只保证已知部分入账，"
            "精确核对不适用（用量完整性看入口 metadata 的 usage_complete）",
            pending=True,
        )
    if unknown_calls > 0:
        return Check(
            "账本核对",
            None,
            f"入口 metadata 标了 {unknown_calls} 个未知调用：总和可能不完整，"
            "待人工确认（usage_status=unknown 的叶子见上）",
            pending=True,
        )

    ledger_prompt = int(ledger.get("prompt_tokens") or 0)
    ledger_completion = int(ledger.get("completion_tokens") or 0)
    try:
        ledger_cost = float(ledger.get("cost_cny") or 0.0)
    except (TypeError, ValueError):
        ledger_cost = 0.0
    prompt_sum = sum(generation_tokens(item)[0] for item in generations)
    completion_sum = sum(generation_tokens(item)[1] for item in generations)
    cost_sum = sum(generation_cost(item) for item in generations)
    prompt_diff = prompt_sum - ledger_prompt
    completion_diff = completion_sum - ledger_completion
    cost_diff = cost_sum - ledger_cost
    ok = (
        prompt_diff == 0
        and completion_diff == 0
        and abs(cost_diff) <= LEDGER_COST_TOLERANCE_CNY
    )
    detail = (
        f"云端 tokens {prompt_sum}+{completion_sum} / 成本 {cost_sum:.4f} 元 vs "
        f"落库 {ledger_prompt}+{ledger_completion} / {ledger_cost:.4f} 元"
        f"（token 差 {prompt_diff:+d}/{completion_diff:+d}，"
        f"成本差 {cost_diff:+.4f}，容差 ±{LEDGER_COST_TOLERANCE_CNY}）"
    )
    return Check("账本核对", ok, detail)


def verify_trace(
    trace: dict[str, Any],
    *,
    expected_latency_ms: int | None = None,
    tolerance_ms: int = DEFAULT_TOLERANCE_MS,
    ledger: dict[str, Any] | None = None,
    expect: list[str] | None = None,
) -> list[Check]:
    """对一条云端 trace 跑全部核对口径；返回逐条结果（纯函数，可离线单测）。

    `expect`：声明本轮**应有**的环节（tool / vision / no-vision / workers），
    缺了对应项从"跳过"升级为"失败"。
    """
    expects = set(expect or [])
    observations = [
        item for item in (trace.get("observations") or []) if isinstance(item, dict)
    ]
    by_id = _by_id(observations)
    roots = [item for item in observations if observation_parent(item) not in by_id]
    root_names = "、".join(observation_name(item) for item in roots) or "（空 trace）"
    checks: list[Check] = []

    # 0. 引用完整：任何 observation 的 parent 都必须在本 trace 内（孤儿 = 链路残缺）。
    #    云端按页返回应自洽；出现孤儿说明导出被截断或上报断链，必须暴露而不是默默画树。
    orphans = [
        f"{observation_name(item)}({observation_id(item)})"
        for item in observations
        if observation_parent(item) and observation_parent(item) not in by_id
    ]
    checks.append(
        Check(
            "引用完整",
            not orphans,
            f"共 {len(observations)} 个 observation，物理根 = {root_names}"
            + (f"；孤儿引用：{'、'.join(orphans)}" if orphans else ""),
        )
    )

    # 1. 入口 span：优先对话轮次（chat.turn）且必须唯一；API 直连链路的入口是 analyze。
    #    注意不能按返回顺序取第一个——云端列表顺序不保证（实测 analyze 会排在 chat.turn 前）
    entries = [item for item in observations if observation_name(item) == "chat.turn"]
    entry = entries[0] if len(entries) == 1 else None
    style = "对话轮次"
    if entry is None and not entries:
        entry = next(
            (item for item in observations if observation_name(item) == "analyze"), None
        )
        style = "API 分析链路"
    if len(entries) > 1:
        checks.append(
            Check(
                "入口 span",
                False,
                f"出现 {len(entries)} 个 chat.turn（串轮 / 重复上报）：ids = "
                f"{'、'.join(observation_id(item) for item in entries)}",
            )
        )
    else:
        checks.append(
            Check(
                "入口 span",
                entry is not None,
                f"入口 = {style}（{observation_name(entry)}）；物理根 = {root_names}；"
                f"共 {len(observations)} 个 observation"
                if entry is not None
                else f"没找到 chat.turn / analyze；物理根 = {root_names}",
            )
        )

    raw_metadata = entry.get("metadata") if entry is not None else None
    entry_metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    turn_status = str(entry_metadata.get("status") or "")
    unknown_calls = int(entry_metadata.get("usage_unknown_calls") or 0)

    generations = [
        item for item in observations if observation_type(item) == "GENERATION"
    ]
    visions = [item for item in generations if is_vision_generation(item)]

    # 2. 工具 span 挂在轮次根下（对话轮次）；--expect tool 时"没有"也算失败
    tools = [
        item for item in observations if observation_name(item).startswith("tool.")
    ]
    if not tools:
        hard = "tool" in expects
        checks.append(
            Check(
                "工具 span",
                False if hard else None,
                (
                    "声明了 --expect tool，但"
                    if hard
                    else "本轮无工具 span（纯回复轮次）"
                )
                + ("没有工具 span" if hard else ""),
            )
        )
    else:
        bad = [
            observation_name(item)
            for item in tools
            if entry is None
            or observation_name(entry) not in ancestor_names(item, by_id)
        ]
        checks.append(
            Check(
                "工具 span",
                not bad,
                f"{len(tools)} 个：{'、'.join(observation_name(t) for t in tools)}"
                + (f"；断链：{'、'.join(bad)}" if bad else ""),
            )
        )

    # 3. worker / 分析节点：对话轮次下必须挂在提交分析的工具 span 下（设计约定 §3）
    workers = [
        item
        for item in observations
        if observation_name(item).startswith(("worker.", "node."))
        or (
            observation_name(item) == "analyze"
            and entry is not None
            and observation_id(item) != observation_id(entry)
        )
    ]
    if not workers:
        hard = "workers" in expects
        checks.append(
            Check(
                "worker 链",
                False if hard else None,
                (
                    "声明了 --expect workers，但没有 worker / 节点 span"
                    if hard
                    else "本轮没有 worker / 节点 span（未提交分析）"
                ),
            )
        )
    elif style != "对话轮次":
        checks.append(
            Check(
                "worker 链",
                None,
                f"API 风格 trace（入口 = analyze）：{len(workers)} 个 worker / 节点"
                " span 不在本脚本硬核对范围，层级请看下面的 span 树",
            )
        )
    else:
        bad = []
        for item in workers:
            chain = ancestor_names(item, by_id)
            if not any(name.startswith("tool.") for name in chain):
                bad.append(f"{observation_name(item)}（链：{' → '.join(chain)}）")
        checks.append(
            Check(
                "worker 链",
                not bad,
                f"{len(workers)} 个 worker / 节点 span"
                + (f"；未挂工具 span：{bad}" if bad else ""),
            )
        )

    # 4. 视觉调用：--expect vision / no-vision 把"有没有"变成硬口径（R6 / OBS-06）
    if "vision" in expects:
        checks.append(
            Check(
                "视觉调用",
                bool(visions),
                f"期望视觉调用：找到 {len(visions)} 个"
                + (
                    f"（{'、'.join(observation_name(v) for v in visions)}）"
                    if visions
                    else f"；期望的名字：{'、'.join(VISION_GENERATIONS)}"
                ),
            )
        )
    elif "no-vision" in expects:
        checks.append(
            Check(
                "视觉调用",
                not visions,
                "期望无视觉调用"
                + (
                    f"，但出现：{'、'.join(observation_name(v) for v in visions)}"
                    if visions
                    else "：确认没有视觉 generation"
                ),
            )
        )
    elif visions:
        checks.append(
            Check(
                "视觉调用",
                None,
                f"出现 {len(visions)} 个视觉 generation"
                f"（{'、'.join(observation_name(v) for v in visions)}）"
                "；未声明 --expect，不作硬口径",
            )
        )

    # 5. 叶子 generation 的用量 / 成本：unknown 只认 metadata.usage_status（R6 修正）
    if not generations:
        checks.append(Check("叶子用量", None, "trace 里没有 generation"))
    else:
        bad_usage = [
            observation_name(item)
            for item in generations
            if usage_status_of(item) != "unknown"
            and (not usage_values_valid(item) or not has_cost(item))
        ]
        unknown = [
            observation_name(item)
            for item in generations
            if usage_status_of(item) == "unknown"
        ]
        detail = (
            f"{len(generations)} 个 generation；"
            f"{sum(1 for item in generations if usage_values_valid(item))} 个用量有效"
            + (f"；缺有效用量或成本：{'、'.join(bad_usage)}" if bad_usage else "")
            + (
                f"；{len(unknown)} 个如实标未知：{'、'.join(unknown)}"
                if unknown
                else ""
            )
        )
        if bad_usage:
            checks.append(Check("叶子用量", False, detail))
        elif unknown:
            checks.append(Check("叶子用量", None, detail, pending=True))
        else:
            checks.append(Check("叶子用量", True, detail))

    # 6. 账本核对（落库三件套 vs 云端求和）
    checks.append(
        _ledger_check(
            generations, ledger, unknown_calls=unknown_calls, turn_status=turn_status
        )
    )

    # 7. 耗时口径：入口 span 耗时（不是 trace 墙钟——并行 worker 会拉宽墙钟）
    entry_latency = observation_latency_ms(entry) if entry is not None else None
    if expected_latency_ms is None:
        checks.append(
            Check(
                "耗时口径",
                None,
                "未提供落库 latency_ms（--trace-id 直连或该轮次没落耗时）",
            )
        )
    elif entry_latency is None:
        checks.append(
            Check(
                "耗时口径", None, "入口 span 没有可算耗时（缺起止时间）", pending=True
            )
        )
    else:
        diff = abs(entry_latency - expected_latency_ms)
        checks.append(
            Check(
                "耗时口径",
                diff <= tolerance_ms,
                f"入口 span {entry_latency} ms vs 落库 {expected_latency_ms} ms（差 {diff} ms，"
                f"容差 {tolerance_ms} ms；trace 墙钟 {trace_wall_ms(observations)} ms 仅供参考）",
            )
        )
    return checks


# ---------------------------------------------------------------------------
# 云端与库（薄封装，不进单测）
# ---------------------------------------------------------------------------


def load_trace(
    cfg: AppConfig, trace_id: str, *, retries: int = DEFAULT_RETRIES
) -> dict[str, Any] | None:
    """拉一条 trace 的 observations，组成 trace 详情同形的 dict；查不到返回 None。

    用新组织唯一可用的 `GET /api/public/v2/observations`（旧版 `/api/public/traces/{id}` 对
    2026-09-16 之后创建的组织返回 410），游标分页取全；`fields` 显式加 `usage,cost,metadata`
    ——默认字段组里没有 `usageDetails` / `costDetails`，叶子用量就核不了。
    404 / 429 按 `retries` 有界重试（退避 5s × 次数）：云导出有延迟，不把"还没收到"
    当成"没上报"；重试耗尽仍 404 返回 None、仍 429 抛 RuntimeError。
    """
    public, secret, host = cfg.langfuse_keys
    url = f"{host.rstrip('/')}/api/public/v2/observations"
    params: dict[str, Any] = {
        "traceId": trace_id,
        "limit": 100,
        "fields": "core,basic,usage,cost,metadata",
    }
    observations: list[dict[str, Any]] = []
    cursor = ""
    for _page in range(MAX_PAGES):
        query = dict(params, cursor=cursor) if cursor else dict(params)
        response: httpx.Response | None = None
        for attempt in range(retries + 1):
            response = httpx.get(url, params=query, auth=(public, secret), timeout=30.0)
            if response.status_code in (404, 410) and attempt < retries:
                time.sleep(5.0 * (attempt + 1))
                continue
            if response.status_code == 429 and attempt < retries:
                time.sleep(5.0 * (attempt + 1))
                continue
            break
        assert response is not None
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
            row = (
                (
                    await session.execute(
                        text(
                            "select trace_id, status, latency_ms, cost_cny, prompt_tokens, "
                            "completion_tokens from chat_messages "
                            "where turn_id = :turn_id order by created_at desc limit 1"
                        ),
                        {"turn_id": turn_id},
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None
    finally:
        await engine.dispose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按轮次 / trace 读回并核对调用链")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--turn-id", help="对话轮次 id（从 chat_messages.trace_id 取 trace）"
    )
    group.add_argument("--trace-id", help="直接指定 trace id")
    parser.add_argument(
        "--expect",
        default="",
        help=f"声明本轮应有的环节，逗号分隔：{'/'.join(EXPECT_CHOICES)}",
    )
    parser.add_argument(
        "--tolerance-ms",
        type=int,
        default=DEFAULT_TOLERANCE_MS,
        help=f"入口 span 耗时容差（默认 {DEFAULT_TOLERANCE_MS}）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"云端 404/429 重试次数（默认 {DEFAULT_RETRIES}）",
    )
    parser.add_argument("--save", help="把云端原始 JSON 落到该路径（验收证据）")
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="输出机器可读结果"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    expects = [
        part.strip() for part in str(args.expect or "").split(",") if part.strip()
    ]
    bad = [item for item in expects if item not in EXPECT_CHOICES]
    if bad:
        print(f"[用法错误] --expect 里的 {'、'.join(bad)} 不在 {EXPECT_CHOICES} 中")
        return 2
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2

    ledger: dict[str, Any] | None = None
    expected_latency_ms: int | None = None
    trace_id = args.trace_id or ""
    if args.turn_id:
        row = asyncio.run(_lookup_turn(cfg, args.turn_id))
        if row is None:
            print(f"[未找到] 没有 turn_id = {args.turn_id} 的消息")
            return 2
        trace_id = str(row.get("trace_id") or "")
        expected_latency_ms = int(row["latency_ms"]) if row.get("latency_ms") else None
        ledger = {
            "cost_cny": row.get("cost_cny"),
            "prompt_tokens": row.get("prompt_tokens"),
            "completion_tokens": row.get("completion_tokens"),
        }
        if not trace_id:
            print(
                f"[没有证据] 该轮次 status = {row.get('status')}，但没关联 trace_id"
                "（追踪关闭或轮次早于 S7.21，属预期情况）"
            )
            return 1

    public, secret, host = cfg.langfuse_keys
    if not (public and secret and host):
        print("[配置错误] LANGFUSE 三件套不齐，没法读回云端 trace")
        return 2

    try:
        trace = load_trace(cfg, trace_id, retries=max(0, int(args.retries)))
    except RuntimeError as exc:  # 429 等云端限流：给出可照做的提示
        print(f"[稍后重试] {exc}")
        return 1
    if trace is None:
        print(f"[还没收到] 云端没有 trace {trace_id}——导出有延迟，稍后重试")
        return 1

    if args.save:
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save).write_text(
            json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[已存证] {args.save}")

    checks = verify_trace(
        trace,
        expected_latency_ms=expected_latency_ms,
        tolerance_ms=args.tolerance_ms,
        ledger=ledger,
        expect=expects,
    )
    observations = [
        item for item in (trace.get("observations") or []) if isinstance(item, dict)
    ]
    failures = [c for c in checks if c.ok is False]
    pending = [c for c in checks if c.ok is None and c.pending]

    if args.as_json:
        print(
            json.dumps(
                {
                    "trace_id": trace_id,
                    "passed": not failures and not pending,
                    "failures": [
                        {"name": c.name, "detail": c.detail} for c in failures
                    ],
                    "pending": [{"name": c.name, "detail": c.detail} for c in pending],
                    "checks": [
                        {
                            "name": c.name,
                            "ok": c.ok,
                            "pending": c.pending,
                            "detail": c.detail,
                        }
                        for c in checks
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"trace {trace_id}（{len(observations)} 个 observation）\n")
        for check in checks:
            mark = (
                ("待验证" if check.pending else "跳过")
                if check.ok is None
                else ("通过" if check.ok else "失败")
            )
            print(f"[{mark}] {check.name}：{check.detail}")
        print("\nspan 树（按开始时间）：")
        for line in tree_lines(observations):
            print(f"  {line}")

    if failures:
        print(f"\n有 {len(failures)} 项没通过；逐条看上面对应的 detail。")
        return 1
    if pending:
        print(
            f"\n有 {len(pending)} 项待验证（unknown 用量 / 缺一边证据）："
            "不算失败，但也不能视为完整通过，请人工确认后重跑。"
        )
        return 3
    print("\n核对完成：硬口径全部通过（跳过项 = 本轮没有对应环节）。")
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口
    sys.exit(main())
