"""完整请求预算与成组裁剪的离线单测（S7.26）。

不联网、不连库：预算判定与裁剪都是纯函数。用例钉住四条硬口径：

1. 估算器与 `tools/llm.py::estimate_tokens` 一致（两处实现不能漂移）；
2. 只保留**连续完整轮次后缀**，遇到超限组就停，不跳过去找更早的短消息；
3. 工具调用与结果必须成组（孤立结果 / 缺结果都要报出来）；
4. 必要内容超限时不放行（`required_context_overflow`），不静默截断。
"""

from __future__ import annotations

import json

from xhs_agent.core import budget
from xhs_agent.tools import llm


def msg(role: str, content: str, **extra: object) -> dict[str, object]:
    return {"role": role, "content": content, **extra}


class TestEstimator:
    def test_matches_llm_estimator(self) -> None:
        for text in ("", "abcde", "中" * 150, "混合 mixed 文本" * 7):
            assert budget.estimate_tokens(text) == llm.estimate_tokens(text)

    def test_messages_include_protocol_overhead(self) -> None:
        one = budget.estimate_messages([msg("user", "abc")])
        assert one == budget.estimate_tokens("abc") + budget.PROTOCOL_OVERHEAD_PER_MESSAGE

    def test_tools_are_serialized_in_stable_order(self) -> None:
        tools = [{"a": 1, "b": {"x": 2, "y": 3}}]
        assert budget.estimate_tools(tools) == budget.estimate_tools(
            [{"b": {"y": 3, "x": 2}, "a": 1}]
        )


class TestGrouping:
    def test_groups_start_at_user_messages(self) -> None:
        history = [msg("assistant", "开场"), msg("user", "u1"), msg("assistant", "a1"),
                   msg("user", "u2"), msg("assistant", "a2")]
        groups = budget.group_turns(history)
        assert [len(group) for group in groups] == [1, 2, 2]
        assert groups[0][0]["content"] == "开场"

    def test_orphan_tool_result_is_reported(self) -> None:
        messages = [msg("user", "u"), msg("tool", "结果", tool_call_id="call-1")]
        problems = budget.validate_tool_pairs(messages)
        assert problems and "孤立" in problems[0]

    def test_missing_tool_result_is_reported(self) -> None:
        messages = [
            msg("user", "u"),
            msg("assistant", "", tool_calls=[{"id": "call-1"}]),
        ]
        problems = budget.validate_tool_pairs(messages)
        assert problems and "没有结果" in problems[0]

    def test_complete_pair_passes(self) -> None:
        messages = [
            msg("user", "u"),
            msg("assistant", "", tool_calls=[{"id": "call-1"}]),
            msg("tool", "ok", tool_call_id="call-1"),
        ]
        assert budget.validate_tool_pairs(messages) == []


class TestFitHistory:
    def test_keeps_only_contiguous_suffix(self) -> None:
        history = [
            msg("user", "旧" * 200),
            msg("assistant", "旧答" * 200),
            msg("user", "新"),
            msg("assistant", "新答"),
        ]
        kept, dropped_groups, kept_groups = budget.fit_history(history, max_tokens=30)
        assert [item["content"] for item in kept] == ["新", "新答"]
        assert dropped_groups == 1 and kept_groups == 1

    def test_stops_instead_of_skipping_oversized_group(self) -> None:
        history = [
            msg("user", "很小"),
            msg("assistant", "也很小"),
            msg("user", "大" * 400),          # 最近一组超限
            msg("assistant", "大答" * 400),
        ]
        kept, dropped_groups, kept_groups = budget.fit_history(history, max_tokens=20)
        # 不跳过超限组去找更早的短消息
        assert kept == []
        assert dropped_groups == 2 and kept_groups == 0

    def test_zero_budget_keeps_nothing(self) -> None:
        kept, dropped, kept_groups = budget.fit_history([msg("user", "x")], max_tokens=0)
        assert kept == [] and dropped == 1 and kept_groups == 0


class TestCompactToolMessage:
    def test_short_message_is_untouched(self) -> None:
        raw = json.dumps({"run_id": "r1"}, ensure_ascii=False)
        assert budget.compact_tool_message(raw) == raw

    def test_long_message_keeps_identifiers_and_marks_truncation(self) -> None:
        raw = json.dumps(
            {
                "run_id": "run-1",
                "version_id": "ver-2",
                "items": [{"text": "x" * 500} for _ in range(20)],
                "body": "长" * 5000,
            },
            ensure_ascii=False,
        )
        compacted = json.loads(budget.compact_tool_message(raw, limit=300))
        assert compacted["run_id"] == "run-1"
        assert compacted["version_id"] == "ver-2"
        assert compacted["truncated"] is True
        assert compacted["truncated_fields"]

    def test_non_json_falls_back_to_preview(self) -> None:
        compacted = json.loads(budget.compact_tool_message("x" * 900, limit=200))
        assert compacted["truncated"] is True and compacted["preview"]


class TestDecide:
    def test_allowed_when_under_limit(self) -> None:
        decision = budget.decide(
            messages=[msg("system", "规则"), msg("user", "问题")],
            tools=None,
            output_reserve=100,
            application_limit=65536,
        )
        assert decision.allowed
        assert decision.safety >= budget.SAFETY_MIN
        assert decision.parts["system"] > 0 and decision.parts["history"] > 0

    def test_overflow_is_denied_with_reason(self) -> None:
        decision = budget.decide(
            messages=[msg("system", "长" * 4000), msg("user", "问题" * 2000)],
            tools=[{"name": "t", "description": "x" * 2000}],
            output_reserve=2048,
            application_limit=3000,
        )
        assert decision.allowed is False
        assert decision.reason == "required_context_overflow"

    def test_effective_limit_uses_min_of_app_and_window(self) -> None:
        decision = budget.decide(
            messages=[msg("user", "x")], tools=None, output_reserve=10,
            application_limit=10000, model_window=1000,
        )
        assert decision.effective_limit == 1000

    def test_decide_with_history_reports_dropped_groups(self) -> None:
        decision = budget.decide_with_history(
            system_messages=[msg("system", "规则")],
            history=[msg("user", "旧" * 300), msg("assistant", "旧答" * 300),
                     msg("user", "新"), msg("assistant", "新答")],
            current=[msg("user", "本轮")],
            tools=None,
            output_reserve=100,
            application_limit=65536,
            history_budget=40,
        )
        assert decision.allowed
        assert decision.dropped_turns == 1
        assert [item["content"] for item in decision.history_messages] == ["新", "新答"]

    def test_check_tool_structure_raises(self) -> None:
        try:
            budget.check_tool_structure([msg("tool", "孤立", tool_call_id="c")])
        except budget.BudgetError as exc:
            assert exc.reason == "history_group_broken"
        else:  # pragma: no cover
            raise AssertionError("孤立结果必须抛错")


class TestPolicy:
    def test_off_skips_checks(self) -> None:
        assert budget.BudgetPolicy().checks is False
        assert budget.BudgetPolicy(mode="shadow").checks is True
        assert budget.BudgetPolicy(mode="shadow").enforces is False
        assert budget.BudgetPolicy(mode="enforce").enforces is True
