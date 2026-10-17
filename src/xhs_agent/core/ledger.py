"""轮次账目投影（E7OBS4-02）：把一轮的模型记录、工具调用与工具内部组件投影成**一份**
可直接写库 / 发事件的账目事实。

用途：成功返回、工作流内部异常、外层异常、实际取消四条收尾路径共用同一份纯函数投影，
      避免"取消记得住、普通异常记不住"的路径差异；同时为未完成的工具调用生成满足
      API 契约的部分记录（真实 args / call_id / attempt / 状态）。
输入：轮次账本字典——`records`（supervisor 模型记录）、`calls`（按 call_id 登记的工具调用
      草稿，调用开始时写入）、`tool_calls`（已完成的工具记录）、`components`（工具内部
      已完成子步骤的检查点事实）、`versions`、`run_id`。
输出：`{tool_calls, run_id, prompt_versions, cost_cny, prompt_tokens, completion_tokens,
      open_calls}`——`tool_calls` 含已完成记录 + 未完成调用的**部分记录**；`open_calls`
      是未完成调用的条数（供完整性核算区分"未知调用"与外层失败的那次调用）。
口径：
- 一个工具调用只贡献**一次**账目：已完成取工具自己的汇总（aggregate），未完成取组件的
  已知合计；同一组件不会被重复累加。
- 组件缺用量来源按 `unknown` 处理；有 estimated → estimated；全部 cached → cached；
  其余实际调用 → actual。`submission` 组件代表分析已提交、worker 用量未知。
- 纯函数：不修改入参、不读库、不请求模型、不补造历史事实。
"""

from __future__ import annotations

import uuid
from typing import Any

#: 未完成调用的默认终态（取消）；失败路径由调用方传入 failed。
OPEN_STATUS = ("failed", "interrupted")


def _facts_total(items: list[dict[str, Any]]) -> tuple[float, int, int]:
    """一组组件 / 记录的已知合计：成本、输入 tokens、输出 tokens。"""
    return (
        round(sum(float(item.get("cost_cny") or 0) for item in items), 4),
        sum(int(item.get("prompt_tokens") or 0) for item in items),
        sum(int(item.get("completion_tokens") or 0) for item in items),
    )


def _facts_usage_status(items: list[dict[str, Any]]) -> str:
    """按组件事实汇总用量来源：unknown > estimated > cached > actual。

    缺来源（空串）的组件按 unknown——它代表"有调用事实、无法解释用量"，不能被同窗口
    的 actual 掩盖；`submission` 组件同样意味着 worker 可能已消费而用量未知。
    """
    if not items:
        return ""
    statuses = [str(item.get("usage_status") or "") for item in items]
    if any(status in ("", "unknown") for status in statuses):
        return "unknown"
    if any(status == "estimated" for status in statuses):
        return "estimated"
    if all(status == "cached" for status in statuses):
        return "cached"
    return "actual"


def _uuid_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return str(uuid.UUID(text))
    except (TypeError, ValueError):
        return None


def _partial_record(
    draft: dict[str, Any], items: list[dict[str, Any]], open_status: str
) -> dict[str, Any]:
    """未完成工具调用的部分记录：真实调用身份 + 组件已知账目，状态如实。"""
    cost, prompt, completion = _facts_total(items)
    run_id = _uuid_or_none(
        draft.get("run_id")
        or next((item.get("run_id") for item in items if item.get("run_id")), "")
        or None
    )
    return {
        "tool": str(draft.get("tool") or ""),
        "status": open_status,
        "args": dict(draft.get("args") or {}),
        "call_id": str(draft.get("call_id") or ""),
        "attempt": max(1, int(draft.get("attempt") or 1)),
        "stage_key": str(draft.get("stage_key") or ""),
        "run_id": run_id,
        "cost_cny": cost,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "usage_status": _facts_usage_status(items),
        "latency_ms": max(0, int(draft.get("latency_ms") or 0)),
        "error": "",
        "steps": [
            dict(step) for step in (draft.get("steps") or []) if isinstance(step, dict)
        ],
    }


def project(
    ledger: dict[str, Any] | None, *, open_status: str = "interrupted"
) -> dict[str, Any]:
    """把轮次账本投影成一份账目事实（四条收尾路径共用；纯函数）。"""
    if open_status not in OPEN_STATUS:
        raise ValueError(f"未完成调用的终态只能是 {OPEN_STATUS}，收到：{open_status!r}")
    data = ledger or {}
    records = [
        dict(item) for item in (data.get("records") or []) if isinstance(item, dict)
    ]
    completed = [
        dict(item) for item in (data.get("tool_calls") or []) if isinstance(item, dict)
    ]
    drafts = {
        str(call_id): dict(draft)
        for call_id, draft in (data.get("calls") or {}).items()
        if isinstance(draft, dict)
    }
    components = [
        dict(item)
        for item in (data.get("components") or {}).values()
        if isinstance(item, dict)
    ]
    by_scope: dict[str, list[dict[str, Any]]] = {}
    for item in components:
        by_scope.setdefault(str(item.get("scope") or ""), []).append(item)

    partial: list[dict[str, Any]] = []
    open_scopes: set[str] = set()
    for call_id, draft in drafts.items():
        scope = str(draft.get("scope") or call_id)
        partial.append(_partial_record(draft, by_scope.get(scope, []), open_status))
        open_scopes.add(scope)

    # 兜底：只登记了组件、没有调用草稿的范围（旧测试 / 兼容 runner）也按未完成调用记账，
    # 否则这些已知消费会凭空消失。缺少 args 时如实为空对象，不虚构参数内容。
    represented = open_scopes | {str(item.get("call_id") or "") for item in completed}
    for scope, items in by_scope.items():
        if not scope or scope in represented:
            continue
        tool = str(next((item.get("tool") for item in items if item.get("tool")), ""))
        if not tool:
            continue
        partial.append(
            _partial_record(
                {"tool": tool, "call_id": scope, "attempt": 1, "args": {}},
                items,
                open_status,
            )
        )
        represented.add(scope)

    tool_calls = [*completed, *partial]
    cost, prompt, completion = _facts_total([*records, *tool_calls])
    run_id = str(data.get("run_id") or "")
    if not run_id:
        run_id = str(
            next((item.get("run_id") for item in components if item.get("run_id")), "")
            or ""
        )
    return {
        "tool_calls": tool_calls,
        "run_id": run_id,
        "prompt_versions": dict(data.get("versions") or {}),
        "cost_cny": cost,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "open_calls": len(partial),
    }
