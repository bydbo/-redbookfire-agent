"""对话编排图的离线单测（S7.3）：直接回答 / 调工具 / 轮次上限 / 失败路径。

provider、工具执行器、事件回调全部注入，不联网不连库。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from xhs_agent.agents.supervisor import TASK_ID, initial_messages
from xhs_agent.config import AppConfig, load_config
from xhs_agent.services.chat_tools import TOOL_RUN_ANALYSIS, TOOLS, ToolOutcome
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMResult
from xhs_agent.workflows.chat import run_turn


def write_config(tmp_path: Path, body: str = "") -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n' + body,
        encoding="utf-8",
    )
    return load_config(str(path))


class ScriptedProvider(llm.BaseProvider):
    """按剧本回应：每一步给出正文、工具调用与可选的分片。"""

    name = "scripted"

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        super().__init__(model="scripted-1")
        self.steps = list(steps)
        self.calls: list[llm.LLMCall] = []

    async def complete(self, call: llm.LLMCall) -> LLMResult:      # pragma: no cover
        raise AssertionError("对话用例不该走 complete()")

    async def complete_with_tools(self, call: llm.LLMCall, on_delta=None) -> LLMResult:
        self.calls.append(call)
        step = self.steps.pop(0) if self.steps else {"text": "（剧本用完了）"}
        text = str(step.get("text") or "")
        pieces = step.get("deltas")
        if on_delta is not None and pieces:
            for piece in pieces:
                await on_delta(piece)
        return LLMResult(text=text, provider=self.name, model=self.model,
                         tool_calls=list(step.get("tool_calls") or []),
                         prompt_tokens=10, completion_tokens=5, cost_cny=0.001)


def tool_call(name: str = TOOL_RUN_ANALYSIS, arguments: str = '{"hotspot": "夜跑"}',
              call_id: str = "call-1") -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": arguments}


class FakeTools:
    """假工具执行器：记账 + 按需返回结果。"""

    def __init__(self, outcome: ToolOutcome | None = None) -> None:
        self.outcome = outcome or ToolOutcome(
            status="succeeded",
            payload={"run_id": "r-1", "candidates": 5, "cost_cny": 0.05,
                     "top1_title": "球拍与手部特写"},
            steps=[{"stage": "完成", "done": 100, "total": 100, "at": "t"}])
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.progress: list[str] = []

    async def __call__(self, name: str, args: dict[str, Any], *,
                       on_progress=None) -> ToolOutcome:
        self.calls.append((name, args))
        if on_progress is not None:
            await on_progress("解析图片", 0, 0)
            await on_progress("分析中", 50, 100)
            self.progress.extend(["解析图片", "分析中"])
        return self.outcome


class Events:
    """收集 on_event 回调，供断言事件序列。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event: str, payload: dict[str, Any]) -> None:
        self.items.append((event, payload))

    def names(self) -> list[str]:
        return [name for name, _payload in self.items]


async def run(cfg: AppConfig, *, steps: list[dict[str, Any]], tools: FakeTools | None = None,
              events: Events | None = None, user_text: str = "帮我蹭一下这个热点",
              history: list[dict[str, Any]] | None = None,
              max_rounds: int | None = None) -> tuple[Any, ScriptedProvider, FakeTools]:
    provider = ScriptedProvider(steps)
    runner = tools or FakeTools()
    turn = await run_turn(cfg, provider=provider, history=history or [], user_text=user_text,
                          run_tool=runner, on_event=events, max_rounds=max_rounds)
    return turn, provider, runner


class TestMessages:
    def test_initial_messages_layout(self, tmp_path: Path) -> None:
        rendered, messages = initial_messages(
            [{"role": "user", "content": "你好"},
             {"role": "assistant", "content": "在的"},
             {"role": "assistant", "content": "   "}],      # 生成中的空行不进上下文
            "帮我看看这张图", attachment_note="1 张图片（热点.png）")

        assert rendered.task_id == TASK_ID and rendered.version == 1
        assert messages[0]["role"] == "system" and messages[0]["content"] == rendered.system
        assert messages[1]["role"] == "user" and messages[1]["content"] == rendered.user
        assert [item["content"] for item in messages[2:4]] == ["你好", "在的"]
        assert messages[-1]["content"].startswith("帮我看看这张图")
        assert "[附件] 1 张图片（热点.png）" in messages[-1]["content"]

    def test_blank_text_with_attachment_gets_placeholder(self) -> None:
        _rendered, messages = initial_messages([], "   ", attachment_note="1 张图片")
        assert "没有文字，只有附件" in messages[-1]["content"]


class TestTurn:
    @pytest.mark.asyncio
    async def test_direct_answer_calls_no_tool(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        turn, provider, runner = await run(cfg, steps=[{"text": "把热点文字发我，或者丢张图。"}])

        assert turn.status == "succeeded"
        assert turn.reply == "把热点文字发我，或者丢张图。"
        assert turn.tool_calls == [] and turn.run_id == ""
        assert turn.prompt_versions == {TASK_ID: 1}
        assert turn.cost_cny == pytest.approx(0.001)
        assert len(provider.calls) == 1
        assert provider.calls[0].tools == TOOLS          # 第一轮带着工具清单
        assert runner.calls == []

    @pytest.mark.asyncio
    async def test_tool_call_then_final_answer(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        events = Events()
        tools = FakeTools()
        turn, provider, runner = await run(
            cfg, events=events, tools=tools,
            steps=[{"text": "", "tool_calls": [tool_call()]},
                   {"text": "跑了完整分析，命中 5 条…"}])

        assert turn.reply == "跑了完整分析，命中 5 条…"
        assert turn.run_id == "r-1"
        assert len(turn.tool_calls) == 1
        entry = turn.tool_calls[0]
        assert entry["tool"] == TOOL_RUN_ANALYSIS and entry["status"] == "succeeded"
        assert entry["run_id"] == "r-1" and entry["args"] == {"hotspot": "夜跑"}
        assert entry["cost_cny"] == pytest.approx(0.05)
        assert runner.calls == [(TOOL_RUN_ANALYSIS, {"hotspot": "夜跑"})]
        # 成本 = 两次 supervisor 调用 + 一次分析
        assert turn.cost_cny == pytest.approx(0.002 + 0.05)
        # 事件：工具开始 → 进度 → 结束
        assert events.names() == ["tool_started", "tool_progress", "tool_progress",
                                  "tool_finished"]
        assert events.items[0][1]["args"] == {"hotspot": "夜跑"}
        assert events.items[1][1]["stage"] == "解析图片"
        # 第二轮把工具结果作为 tool 消息喂回去
        second = provider.calls[1].messages
        assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "call-1"
        assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call-1"
        assert json.loads(second[-1]["content"])["run_id"] == "r-1"

    @pytest.mark.asyncio
    async def test_text_deltas_are_forwarded(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        events = Events()
        turn, _provider, _runner = await run(
            cfg, events=events,
            steps=[{"text": "你好世界", "deltas": ["你好", "世界"]}])

        assert turn.reply == "你好世界"
        assert [payload["text"] for name, payload in events.items
                if name == "text_delta"] == ["你好", "世界"]

    @pytest.mark.asyncio
    async def test_round_cap_forces_final_answer_without_tools(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        turn, provider, runner = await run(
            cfg, max_rounds=1,
            steps=[{"text": "", "tool_calls": [tool_call()]},
                   {"text": "按现有结果先给你收个尾。"}])

        assert len(runner.calls) == 1                      # 只调了一次工具
        assert turn.reply == "按现有结果先给你收个尾。"
        assert provider.calls[1].tools is None             # 到上限的那次不带工具
        assert len(provider.calls) == 2

    @pytest.mark.asyncio
    async def test_empty_reply_gets_a_fallback_line(self, tmp_path: Path) -> None:
        """模型什么都没说（也没要工具）时不能给用户空回复。"""
        cfg = write_config(tmp_path)
        turn, _provider, _runner = await run(cfg, steps=[{"text": ""}])

        assert turn.reply and "说清楚一点" in turn.reply
        assert turn.tool_calls == []

    @pytest.mark.asyncio
    async def test_unknown_tool_is_recorded_and_ends_turn(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        turn, provider, runner = await run(
            cfg, steps=[{"text": "", "tool_calls": [tool_call(name="delete_everything")]}])

        assert runner.calls == []
        assert len(turn.tool_calls) == 1
        assert turn.tool_calls[0]["status"] == "failed"
        assert "不存在的工具" in turn.tool_calls[0]["error"]
        assert turn.reply                                     # 不能给用户空回复
        assert len(provider.calls) == 1                        # 不再回头问模型

    @pytest.mark.asyncio
    async def test_tool_failure_is_reported_back_to_the_model(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        tools = FakeTools(ToolOutcome(status="failed", error="素材目录不存在"))
        turn, provider, _runner = await run(
            cfg, tools=tools,
            steps=[{"text": "", "tool_calls": [tool_call()]},
                   {"text": "这次没跑成：素材目录不存在。"}])

        assert turn.tool_calls[0]["status"] == "failed"
        assert turn.tool_calls[0]["error"] == "素材目录不存在"
        assert turn.reply == "这次没跑成：素材目录不存在。"
        payload = json.loads(provider.calls[1].messages[-1]["content"])
        assert payload["status"] == "failed" and "素材目录不存在" in payload["error"]

    @pytest.mark.asyncio
    async def test_bad_arguments_become_a_failed_tool_call(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        turn, provider, runner = await run(
            cfg, steps=[{"text": "", "tool_calls": [tool_call(arguments="{oops")]},
                        {"text": "参数没看懂，我再试一次。"}])

        assert runner.calls == []                            # 参数坏了就不执行
        assert turn.tool_calls[0]["status"] == "failed"
        assert "JSON" in turn.tool_calls[0]["error"]
        assert turn.reply == "参数没看懂，我再试一次。"
        assert len(provider.calls) == 2

    @pytest.mark.asyncio
    async def test_provider_error_returns_failed_turn(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)

        class Broken(llm.BaseProvider):
            name = "broken"

            async def complete(self, call: llm.LLMCall) -> LLMResult:      # pragma: no cover
                raise AssertionError

            async def complete_with_tools(self, call: llm.LLMCall, on_delta=None) -> LLMResult:
                return LLMResult(text="", provider=self.name, model=self.model,
                                 attempts=3, error="HTTP 500: boom")

        turn = await run_turn(cfg, provider=Broken(model="x"), history=[], user_text="嗨",
                              run_tool=FakeTools())

        assert turn.status == "failed"
        assert "HTTP 500" in turn.error
        assert turn.reply == ""
        assert turn.prompt_versions == {TASK_ID: 1}
