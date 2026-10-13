"""多轮记忆与上下文预算（S7.10）：历史装配是确定性能力——不调模型、不猜语义。

口径见 `docs/contracts/配置契约.md` §3.12（`history_max_messages` / `history_token_budget`）与
`agents/supervisor.py::assemble_history` 的 docstring。
"""

from __future__ import annotations

from typing import Any

from xhs_agent.agents.supervisor import (
    DIGEST_CHARS,
    assemble_history,
    build_messages,
    tool_summary_line,
)
from xhs_agent.tools import prompt as prompt_tool
from xhs_agent.tools.llm import estimate_tokens


def msg(role: str, content: str, **extra: Any) -> dict[str, Any]:
    return {"role": role, "content": content, **extra}


def joined(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(item["content"]) for item in messages)


class TestToolSummaryLine:
    """工具摘要行：模型据此拿到 `run_id`，不必等服务端兜底（S7.9 的缺口）。"""

    def test_carries_run_id(self) -> None:
        line = tool_summary_line([{"tool": "run_hotspot_analysis", "status": "succeeded",
                                   "run_id": "r-1"}])
        assert line == "[上一步] run_hotspot_analysis（succeeded） run_id=r-1"

    def test_failed_call_reports_reason(self) -> None:
        line = tool_summary_line([{"tool": "read_run", "status": "failed",
                                   "error": "找不到这次运行"}])
        assert line == "[上一步] read_run（失败：找不到这次运行）"

    def test_empty_when_no_tool_calls(self) -> None:
        assert tool_summary_line([]) == ""
        assert tool_summary_line(None) == ""


class TestAssembleHistory:
    def test_recent_messages_keep_full_text_and_older_ones_digest(self) -> None:
        history = [msg("user", "一" * 100), msg("assistant", "二" * 100),
                   msg("user", "三" * 10)]
        assembled, dropped = assemble_history(history, keep_messages=2,
                                              token_budget=100_000)
        assert dropped == 0
        assert [item["content"] for item in assembled] == [
            "一" * DIGEST_CHARS + "…", "二" * 100, "三" * 10]
        assert [item["role"] for item in assembled] == ["user", "assistant", "user"]

    def test_over_budget_stops_and_reports_dropped(self) -> None:
        history = [msg("user", f"第{i}条" + "字" * 300) for i in range(5)]
        assembled, dropped = assemble_history(history, keep_messages=1, token_budget=250)
        assert dropped == 3
        assert len(assembled) == 2
        assert assembled[-1]["content"] == history[-1]["content"]   # 最新一条保留原文
        assert assembled[0]["content"].endswith("…")                # 更早的降级成摘要
        used = sum(estimate_tokens(item["content"]) for item in assembled)
        assert used <= 250

    def test_newest_message_survives_even_when_it_alone_exceeds_budget(self) -> None:
        history = [msg("user", "字" * 3000)]
        assembled, dropped = assemble_history(history, token_budget=10)
        assert len(assembled) == 1 and dropped == 0
        assert assembled[0]["content"] == "字" * 3000

    def test_tool_result_line_rides_along_in_history(self) -> None:
        history = [msg("assistant", "跑完了", tool_calls=[
            {"tool": "run_hotspot_analysis", "status": "succeeded", "run_id": "r-1"}])]
        assembled, _dropped = assemble_history(history)
        assert assembled[0]["content"] == (
            "跑完了\n[上一步] run_hotspot_analysis（succeeded） run_id=r-1")

    def test_running_assistant_row_is_skipped(self) -> None:
        assembled, dropped = assemble_history([msg("assistant", ""), msg("assistant", "   ")])
        assert assembled == [] and dropped == 0

    def test_cross_session_messages_are_not_merged(self) -> None:
        """只装配传进来的消息——不同会话的历史永不互相出现（跨会话不串味）。"""
        session_a = [msg("user", "甲会话的热点"), msg("assistant", "甲会话的结论")]
        text = joined(assemble_history(session_a)[0])
        assert "甲会话" in text and "乙会话" not in text


class TestBuildMessages:
    @staticmethod
    def _rendered() -> Any:
        return prompt_tool.load("chat_supervisor").render(capabilities="（清单）")

    def test_current_message_keeps_history_and_attachment_notes(self) -> None:
        history = [msg("user", "早前那张图", attachments=[{"kind": "image",
                                                          "name": "热点.png"}])]
        messages = build_messages(self._rendered(), history,
                                  "上面那张图，帮我蹭一下",
                                  attachment_note="1 张图片（新图.png）",
                                  token_budget=1)
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        last = str(messages[-1]["content"])
        assert last.startswith("上面那张图，帮我蹭一下")
        assert "[历史] 本会话早前传过 1 张图（最近一组：热点.png）" in last
        assert "[附件] 1 张图片（新图.png）" in last

    def test_over_budget_note_is_told_to_the_model(self) -> None:
        history = [msg("user", "第一篇" + "字" * 300), msg("user", "第二篇" + "字" * 300)]
        messages = build_messages(self._rendered(), history, "接着说",
                                  keep_messages=1, token_budget=50)
        assert "没装进上下文" in str(messages[-1]["content"])

    def test_blank_text_with_attachment_gets_placeholder(self) -> None:
        messages = build_messages(self._rendered(), [], "   ",
                                  attachment_note="1 张图片")
        assert "没有文字，只有附件" in str(messages[-1]["content"])
