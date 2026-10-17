"""轮次账目投影的纯函数单测（E7OBS4-02）。

覆盖：固定验收样本（0.004 元 / 510·25 tokens）在四条收尾路径口径一致、组件与工具汇总
不双计、缓存不重计、提交前失败不虚构引用、未完成调用生成满足 API 契约的部分记录。
不连库、不联网。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from xhs_agent.api.models import ChatToolCall
from xhs_agent.core import ledger as ledger_projection

RUN_ID = "77777777-7777-4777-8777-777777777777"


def supervisor_record(cost: float = 0.001) -> dict[str, Any]:
    return {
        "task": "chat_supervisor",
        "cost_cny": cost,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "usage_status": "actual",
    }


def draft(call_id: str = "call-1", **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "tool": "run_hotspot_analysis",
        "call_id": call_id,
        "attempt": 1,
        "args": {"hotspot": "某条热点", "topk": 5},
        "scope": call_id,
        "stage_key": "分析中",
        "run_id": "",
        "latency_ms": 1200,
        "steps": [{"stage": "解析图片", "done": 0, "total": 0}, {"stage": "提交分析"}],
    }
    item.update(overrides)
    return item


def image_component(call_id: str = "call-1", **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "scope": call_id,
        "component": "image_parse",
        "tool": "run_hotspot_analysis",
        "cost_cny": 0.003,
        "prompt_tokens": 500,
        "completion_tokens": 20,
        "usage_status": "actual",
        "prompt_versions": {"image_hotspot_clue": 3},
    }
    item.update(overrides)
    return item


def submission_component(call_id: str = "call-1") -> dict[str, Any]:
    """分析已提交：worker 可能已消费，但对话层拿不到用量。"""
    return {
        "scope": call_id,
        "component": "submission",
        "tool": "run_hotspot_analysis",
        "run_id": RUN_ID,
        "job_id": "88888888-8888-4888-8888-888888888888",
        "status": "submitted",
    }


def fixed_sample() -> dict[str, Any]:
    """文档 §4.4 的固定样本：supervisor 0.001/10/5 + 图片 0.003/500/20 + 分析已提交。"""
    return {
        "records": [supervisor_record()],
        "calls": {"call-1": draft()},
        "tool_calls": [],
        "components": {
            "call-1:image_parse": image_component(),
            "call-1:submission": submission_component(),
        },
        "versions": {"chat_supervisor": 2},
        "run_id": "",
    }


def test_fixed_sample_projects_known_totals_once() -> None:
    """B1：已知合计必须是 0.004 元、510/25 tokens，图片版本与合法 run_id 保留。"""
    result = ledger_projection.project(fixed_sample(), open_status="failed")

    assert result["cost_cny"] == pytest.approx(0.004)
    assert result["prompt_tokens"] == 510
    assert result["completion_tokens"] == 25
    assert result["run_id"] == RUN_ID          # 来自 submission 组件的真实引用
    assert result["prompt_versions"] == {"chat_supervisor": 2}
    assert result["open_calls"] == 1

    partial = result["tool_calls"][0]
    assert partial["status"] == "failed"        # 外层异常：如实失败
    assert partial["args"] == {"hotspot": "某条热点", "topk": 5}
    assert partial["call_id"] == "call-1" and partial["attempt"] == 1
    assert partial["run_id"] == RUN_ID
    assert partial["cost_cny"] == pytest.approx(0.003)
    assert (partial["prompt_tokens"], partial["completion_tokens"]) == (500, 20)
    # submission 组件没有用量来源 → 该调用整体 unknown（worker 可能已消费）
    assert partial["usage_status"] == "unknown"
    assert [step["stage"] for step in partial["steps"]] == ["解析图片", "提交分析"]


def test_cancel_only_changes_terminal_status_not_known_totals() -> None:
    """B2：取消只把未完成调用改成 interrupted，已知消费不变。"""
    failed = ledger_projection.project(fixed_sample(), open_status="failed")
    cancelled = ledger_projection.project(fixed_sample(), open_status="interrupted")

    assert cancelled["cost_cny"] == failed["cost_cny"]
    assert cancelled["prompt_tokens"] == failed["prompt_tokens"]
    assert cancelled["tool_calls"][0]["status"] == "interrupted"
    assert failed["tool_calls"][0]["status"] == "failed"


def test_completed_call_is_not_double_counted_with_components() -> None:
    """B3：工具完成后同时存在 aggregate 与组件——只计一次（不双计、不取 max）。"""
    ledger = fixed_sample()
    ledger["calls"] = {}          # 调用已结清
    ledger["tool_calls"] = [
        {
            "tool": "run_hotspot_analysis",
            "status": "succeeded",
            "args": {"hotspot": "某条热点", "topk": 5},
            "call_id": "call-1",
            "attempt": 1,
            "stage_key": "report",
            "run_id": RUN_ID,
            # 工具汇总覆盖了图片组件（0.003 / 500·20）；组件仍留在账本里
            "cost_cny": 0.003,
            "prompt_tokens": 500,
            "completion_tokens": 20,
            "usage_status": "estimated",
            "latency_ms": 3000,
            "error": "",
            "steps": [],
        }
    ]

    result = ledger_projection.project(ledger)

    assert result["cost_cny"] == pytest.approx(0.004)
    assert (result["prompt_tokens"], result["completion_tokens"]) == (510, 25)
    assert result["open_calls"] == 0
    assert len(result["tool_calls"]) == 1


def test_tool_with_components_and_tool_without_both_counted() -> None:
    """B4：一个工具有检查点、另一个没有——两者各计一次，不靠 zip 对齐。"""
    ledger = {
        "records": [supervisor_record()],
        "calls": {
            "call-1": draft("call-1"),
            "call-2": draft(
                "call-2", tool="search_materials", args={"hotspot": "另一条"},
                run_id="", latency_ms=80, steps=[],
            ),
        },
        "tool_calls": [],
        "components": {"call-1:image_parse": image_component()},
        "versions": {},
        "run_id": "",
    }

    result = ledger_projection.project(ledger)

    assert result["open_calls"] == 2
    by_call = {item["call_id"]: item for item in result["tool_calls"]}
    assert by_call["call-1"]["cost_cny"] == pytest.approx(0.003)
    assert by_call["call-2"]["cost_cny"] == 0
    assert by_call["call-2"]["args"] == {"hotspot": "另一条"}
    assert result["cost_cny"] == pytest.approx(0.004)


def test_cached_component_does_not_re_add_history_cost() -> None:
    """B5：缓存命中不重计历史费用，但调用事实（组件）与运行引用保留。"""
    ledger = {
        "records": [supervisor_record()],
        "calls": {"call-1": draft(run_id=RUN_ID)},
        "tool_calls": [],
        "components": {
            # 历史那次真实消费不在本轮账本里；本轮只有 cached 组件
            "call-1:image_parse": image_component(
                cost_cny=0.0, prompt_tokens=0, completion_tokens=0,
                usage_status="cached",
            ),
            "call-1:submission": submission_component(),
        },
        "versions": {},
        "run_id": RUN_ID,
    }

    result = ledger_projection.project(ledger)

    assert result["cost_cny"] == pytest.approx(0.001)
    assert result["tool_calls"][0]["usage_status"] == "unknown"  # submission 仍未解释


def test_failed_before_submission_fabricates_nothing() -> None:
    """B6：提交前失败 / 非计费校验失败——不虚构 run_id，也不额外记一次模型未知。"""
    ledger = {
        "records": [supervisor_record()],
        "calls": {
            "call-1": draft(run_id="", stage_key="", latency_ms=12, steps=[]),
        },
        "tool_calls": [],
        "components": {},
        "versions": {},
        "run_id": "",
    }

    result = ledger_projection.project(ledger, open_status="failed")
    partial = result["tool_calls"][0]

    assert partial["run_id"] is None       # 没提交过：按契约表达空值
    assert partial["cost_cny"] == 0
    assert partial["usage_status"] == ""
    assert result["open_calls"] == 1       # 外层异常不再重复代表这次调用
    assert result["run_id"] == ""


def test_partial_record_satisfies_public_contract() -> None:
    """A1：部分记录必须能通过 `ChatToolCall` 校验（args 必填，旧实现直接 500）。"""
    result = ledger_projection.project(fixed_sample(), open_status="interrupted")

    model = ChatToolCall.model_validate(result["tool_calls"][0])

    assert model.tool == "run_hotspot_analysis"
    assert model.status == "interrupted"
    assert model.args["hotspot"] == "某条热点"
    assert model.call_id == "call-1" and model.attempt == 1
    assert str(model.run_id) == RUN_ID


def test_legacy_component_scope_without_draft_still_counted() -> None:
    """兼容旧 runner：只登记组件、没有调用草稿的已知消费不能凭空消失。"""
    ledger = {
        "records": [],
        "calls": {},
        "tool_calls": [],
        "components": {
            "parse_image_clue:1": {
                "scope": "parse_image_clue:1",
                "component": "image_parse",
                "tool": "parse_image_clue",
                "cost_cny": 0.002,
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "usage_status": "actual",
            }
        },
        "versions": {},
        "run_id": "",
    }

    result = ledger_projection.project(ledger)

    assert result["cost_cny"] == pytest.approx(0.002)
    assert result["tool_calls"][0]["call_id"] == "parse_image_clue:1"
    assert result["tool_calls"][0]["args"] == {}   # 不虚构参数内容


def test_projection_is_pure_and_validates_status() -> None:
    ledger = fixed_sample()
    snapshot = copy.deepcopy(ledger)

    ledger_projection.project(ledger)

    assert ledger == snapshot        # 不修改入参
    with pytest.raises(ValueError):
        ledger_projection.project(ledger, open_status="succeeded")


def test_unknown_usage_is_not_hidden_by_actual_values() -> None:
    """E7OBS4-05：窗口里既有 actual 又有缺来源的组件时，整体必须 unknown。"""
    ledger = {
        "records": [supervisor_record()],
        "calls": {"call-1": draft()},
        "tool_calls": [],
        "components": {
            "call-1:image_parse": image_component(),
            "call-1:clue": {
                "scope": "call-1",
                "component": "clue",
                "tool": "run_hotspot_analysis",
                "cost_cny": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
        },
        "versions": {},
        "run_id": "",
    }

    result = ledger_projection.project(ledger)

    assert result["tool_calls"][0]["usage_status"] == "unknown"
    assert result["cost_cny"] == pytest.approx(0.004)   # 已知数照旧保留
