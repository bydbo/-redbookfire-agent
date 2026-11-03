"""对话编排图的离线单测（S7.3；S7.7a 补"分发不再硬编码"的反证）。

provider、工具执行器、事件回调全部注入，不联网不连库。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from xhs_agent.agents.supervisor import TASK_ID, build_messages, initial_messages
from xhs_agent.config import AppConfig, load_config
from xhs_agent.services import chat_tools
from xhs_agent.services.chat_tools import TOOL_RUN_ANALYSIS, TOOLS, ToolOutcome
from xhs_agent.services.chat_turns import build_tool_runner
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMResult
from xhs_agent.tools.prompt import RenderedPrompt
from xhs_agent.workflows import chat as chat_workflow
from xhs_agent.workflows.chat import run_turn


def write_config(tmp_path: Path, body: str = "") -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\nruns_dir = "{(tmp_path / "runs").as_posix()}"\n' + body,
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

    async def complete(self, call: llm.LLMCall) -> LLMResult:  # pragma: no cover
        raise AssertionError("对话用例不该走 complete()")

    async def complete_with_tools(self, call: llm.LLMCall, on_delta=None) -> LLMResult:
        self.calls.append(call)
        step = self.steps.pop(0) if self.steps else {"text": "（剧本用完了）"}
        text = str(step.get("text") or "")
        pieces = step.get("deltas")
        if on_delta is not None and pieces:
            for piece in pieces:
                await on_delta(piece)
        return LLMResult(
            text=text,
            provider=self.name,
            model=self.model,
            tool_calls=list(step.get("tool_calls") or []),
            prompt_tokens=10,
            completion_tokens=5,
            cost_cny=0.001,
            usage_status="actual",
        )  # R5：假 provider 也按真实契约标来源


def tool_call(
    name: str = TOOL_RUN_ANALYSIS,
    arguments: str = '{"hotspot": "夜跑"}',
    call_id: str = "call-1",
) -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": arguments}


class FakeTools:
    """假工具执行器：记账 + 按需返回结果。"""

    def __init__(self, outcome: ToolOutcome | None = None) -> None:
        self.outcome = outcome or ToolOutcome(
            status="succeeded",
            payload={
                "run_id": "r-1",
                "candidates": 5,
                "cost_cny": 0.05,
                "top1_title": "球拍与手部特写",
            },
            steps=[{"stage": "完成", "done": 100, "total": 100, "at": "t"}],
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.progress: list[str] = []

    async def __call__(
        self, name: str, args: dict[str, Any], *, on_progress=None
    ) -> ToolOutcome:
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


async def run(
    cfg: AppConfig,
    *,
    steps: list[dict[str, Any]] | None = None,
    tools: FakeTools | None = None,
    events: Events | None = None,
    user_text: str = "帮我蹭一下这个热点",
    history: list[dict[str, Any]] | None = None,
    max_rounds: int | None = None,
    provider: ScriptedProvider | None = None,
) -> tuple[Any, ScriptedProvider, FakeTools]:
    provider = provider or ScriptedProvider(steps or [])
    runner = tools or FakeTools()
    turn = await run_turn(
        cfg,
        provider=provider,
        history=history or [],
        user_text=user_text,
        run_tool=runner,
        on_event=events,
        max_rounds=max_rounds,
    )
    return turn, provider, runner


class TestMessages:
    def test_initial_messages_layout(self, tmp_path: Path) -> None:
        rendered, messages = initial_messages(
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "在的"},
                {"role": "assistant", "content": "   "},
            ],  # 生成中的空行不进上下文
            "帮我看看这张图",
            attachment_note="1 张图片（热点.png）",
        )

        assert rendered.task_id == TASK_ID and rendered.version == SUPERVISOR_VERSION
        assert (
            messages[0]["role"] == "system"
            and messages[0]["content"] == rendered.system
        )
        assert messages[1]["role"] == "user" and messages[1]["content"] == rendered.user
        assert [item["content"] for item in messages[2:4]] == ["你好", "在的"]
        assert messages[-1]["content"].startswith("帮我看看这张图")
        assert "[附件] 1 张图片（热点.png）" in messages[-1]["content"]

    def test_blank_text_with_attachment_gets_placeholder(self) -> None:
        _rendered, messages = initial_messages([], "   ", attachment_note="1 张图片")
        assert "没有文字，只有附件" in messages[-1]["content"]

    def test_empty_task_does_not_create_empty_user_message(self) -> None:
        rendered = RenderedPrompt(TASK_ID, SUPERVISOR_VERSION, "系统指令", "  ")
        assert build_messages(rendered, [], "当前问题") == [
            {"role": "system", "content": "系统指令"},
            {"role": "user", "content": "当前问题"},
        ]


class TestTurn:
    @pytest.mark.asyncio
    async def test_direct_answer_calls_no_tool(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        turn, provider, runner = await run(
            cfg, steps=[{"text": "把热点文字发我，或者丢张图。"}]
        )

        assert turn.status == "succeeded"
        assert turn.reply == "把热点文字发我，或者丢张图。"
        assert turn.tool_calls == [] and turn.run_id == ""
        assert turn.prompt_versions == {TASK_ID: SUPERVISOR_VERSION}
        assert turn.cost_cny == pytest.approx(0.001)
        assert len(provider.calls) == 1
        # 第一轮带着工具清单：内容由 [web] 开关决定（PR2），关闭态与旧 TOOLS 逐项相等
        assert provider.calls[0].tools == chat_tools.tools_for(cfg)
        assert runner.calls == []

    @pytest.mark.asyncio
    async def test_turn_sums_tokens_from_supervisor_and_tools(
        self, tmp_path: Path
    ) -> None:
        """S7.10：轮级 tokens = supervisor 各次调用 + 工具内部模型调用（含图片解析/分析 run）。"""
        cfg = write_config(tmp_path)
        tools = FakeTools(
            ToolOutcome(
                status="succeeded",
                payload={
                    "run_id": "r-1",
                    "candidates": 5,
                    "cost_cny": 0.05,
                    "prompt_tokens": 700,
                    "completion_tokens": 300,
                    "usage_status": "actual",
                },
                steps=[{"stage": "完成", "done": 100, "total": 100, "at": "t"}],
            )
        )
        turn, _provider, _runner = await run(
            cfg,
            steps=[{"text": "", "tool_calls": [tool_call()]}, {"text": "跑好了"}],
            tools=tools,
        )

        # ScriptedProvider 每次回 10 / 5，两跳共 20 / 10；工具再贡献 700 / 300
        assert (turn.prompt_tokens, turn.completion_tokens) == (720, 310)
        assert turn.tool_calls[0]["prompt_tokens"] == 700
        assert turn.tool_calls[0]["completion_tokens"] == 300

    @pytest.mark.asyncio
    async def test_tool_call_then_final_answer(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        events = Events()
        tools = FakeTools()
        turn, provider, runner = await run(
            cfg,
            events=events,
            tools=tools,
            steps=[
                {"text": "", "tool_calls": [tool_call()]},
                {"text": "跑了完整分析，命中 5 条…"},
            ],
        )

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
        # 事件：决策阶段（running/succeeded）→ 工具开始 → 进度 → 结束 → 决策阶段（S7.20）
        assert events.names() == [
            "stage",
            "stage",
            "tool_started",
            "tool_progress",
            "tool_progress",
            "tool_finished",
            "stage",
            "stage",
        ]
        assert events.items[0][1]["stage"] == "supervisor"
        assert events.items[0][1]["status"] == "running"
        assert events.items[2][1]["args"] == {"hotspot": "夜跑"}
        assert events.items[2][1]["call_id"] == "call-1"
        assert events.items[2][1]["attempt"] == 1
        assert events.items[3][1]["stage"] == "解析图片"
        assert events.items[3][1]["stage_key"] == "image_parse"
        # 第二轮把工具结果作为 tool 消息喂回去
        second = provider.calls[1].messages
        assert (
            second[-2]["role"] == "assistant"
            and second[-2]["tool_calls"][0]["id"] == "call-1"
        )
        assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call-1"
        assert json.loads(second[-1]["content"])["run_id"] == "r-1"

    @pytest.mark.asyncio
    async def test_text_deltas_are_forwarded(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        events = Events()
        turn, _provider, _runner = await run(
            cfg, events=events, steps=[{"text": "你好世界", "deltas": ["你好", "世界"]}]
        )

        assert turn.reply == "你好世界"
        assert [
            payload["text"] for name, payload in events.items if name == "text_delta"
        ] == ["你好", "世界"]

    @pytest.mark.asyncio
    async def test_round_cap_forces_final_answer_without_tools(
        self, tmp_path: Path
    ) -> None:
        cfg = write_config(tmp_path)
        turn, provider, runner = await run(
            cfg,
            max_rounds=1,
            steps=[
                {"text": "", "tool_calls": [tool_call()]},
                {"text": "按现有结果先给你收个尾。"},
            ],
        )

        assert len(runner.calls) == 1  # 只调了一次工具
        assert turn.reply == "按现有结果先给你收个尾。"
        assert provider.calls[1].tools is None  # 到上限的那次不带工具
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
            cfg,
            steps=[{"text": "", "tool_calls": [tool_call(name="delete_everything")]}],
        )

        assert runner.calls == []
        assert len(turn.tool_calls) == 1
        assert turn.tool_calls[0]["status"] == "failed"
        assert "不存在的工具" in turn.tool_calls[0]["error"]
        assert turn.reply  # 不能给用户空回复
        assert len(provider.calls) == 1  # 不再回头问模型

    @pytest.mark.asyncio
    async def test_tool_failure_is_reported_back_to_the_model(
        self, tmp_path: Path
    ) -> None:
        cfg = write_config(tmp_path)
        tools = FakeTools(ToolOutcome(status="failed", error="素材目录不存在"))
        turn, provider, _runner = await run(
            cfg,
            tools=tools,
            steps=[
                {"text": "", "tool_calls": [tool_call()]},
                {"text": "这次没跑成：素材目录不存在。"},
            ],
        )

        assert turn.tool_calls[0]["status"] == "failed"
        assert turn.tool_calls[0]["error"] == "素材目录不存在"
        assert turn.reply == "这次没跑成：素材目录不存在。"
        payload = json.loads(provider.calls[1].messages[-1]["content"])
        assert payload["status"] == "failed" and "素材目录不存在" in payload["error"]

    @pytest.mark.asyncio
    async def test_bad_arguments_become_a_failed_tool_call(
        self, tmp_path: Path
    ) -> None:
        cfg = write_config(tmp_path)
        turn, provider, runner = await run(
            cfg,
            steps=[
                {"text": "", "tool_calls": [tool_call(arguments="{oops")]},
                {"text": "参数没看懂，我再试一次。"},
            ],
        )

        assert runner.calls == []  # 参数坏了就不执行
        assert turn.tool_calls[0]["status"] == "failed"
        assert "JSON" in turn.tool_calls[0]["error"]
        assert turn.reply == "参数没看懂，我再试一次。"
        assert len(provider.calls) == 2

    @pytest.mark.asyncio
    async def test_provider_error_returns_failed_turn(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)

        class Broken(llm.BaseProvider):
            name = "broken"

            async def complete(
                self, call: llm.LLMCall
            ) -> LLMResult:  # pragma: no cover
                raise AssertionError

            async def complete_with_tools(
                self, call: llm.LLMCall, on_delta=None
            ) -> LLMResult:
                return LLMResult(
                    text="",
                    provider=self.name,
                    model=self.model,
                    attempts=3,
                    error="HTTP 500: boom",
                )

        turn = await run_turn(
            cfg,
            provider=Broken(model="x"),
            history=[],
            user_text="嗨",
            run_tool=FakeTools(),
        )

        assert turn.status == "failed"
        assert "LLMError" in turn.error
        assert "HTTP 500: boom" not in turn.error
        assert turn.reply == ""
        assert turn.prompt_versions == {TASK_ID: SUPERVISOR_VERSION}


# ---------- S7.7a：工具清单与分派都不在图里写死 ----------

FAKE_TOOL = "fake_probe"
FAKE_TASK = "fake_probe_task"
# `chat_supervisor` 当前的 prompt 版本：prompt 升版时这条要跟着改（prompt 契约 §六 要求升版必跑回归）
SUPERVISOR_VERSION = 6


def register_fake_tool(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """往 `chat_tools` 注册一个假工具（清单 + 分发表各一处），返回它收到的参数记录。"""
    seen: list[dict[str, Any]] = []
    schema = {
        "type": "function",
        "function": {
            "name": FAKE_TOOL,
            "description": "用例注册的假工具",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
    monkeypatch.setattr(chat_tools, "TOOLS", [*chat_tools.TOOLS, schema])

    async def handler(
        _ctx: Any,
        args: dict[str, Any],
        *,
        attachments: Any = None,
        on_progress: Any = None,
    ) -> ToolOutcome:
        seen.append(args)
        return ToolOutcome(
            status="succeeded", payload={"ok": True}, prompt_versions={FAKE_TASK: 7}
        )

    monkeypatch.setitem(chat_tools.HANDLERS, FAKE_TOOL, handler)
    return seen


class TestDispatchIsDataDriven:
    """S7.7a：`workflows/chat.py` 里不出现任何具体工具名；注册新工具只改 `chat_tools.py`。"""

    def test_graph_source_contains_no_tool_name(self) -> None:
        source = Path(str(chat_workflow.__file__)).read_text(encoding="utf-8")
        # 扫描范围 = 默认名 ∪ 网页两名：网页 schema 不在 TOOLS 里，只扫 tool_names()
        # 会让「将来有人在 chat.py 里写死 search_hotspots」漏过去。
        default_names = chat_tools.tool_names()
        scanned = default_names | {
            chat_tools.TOOL_SEARCH_HOTSPOTS,
            chat_tools.TOOL_FETCH_URL,
        }
        assert scanned > default_names  # 防范围退化：网页两名不该混进默认清单
        for name in sorted(scanned):
            assert name not in source, f"workflows/chat.py 里写死了工具名：{name}"

    @pytest.mark.asyncio
    async def test_registered_tool_is_dispatched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = register_fake_tool(monkeypatch)
        cfg = write_config(tmp_path)
        # 用**真实**的运行器（build_tool_runner）证明分发表确实把假工具接上了
        runner = build_tool_runner(
            cast(Any, None),
            cfg=cfg,
            client=cast(Any, None),
            session_id=uuid.uuid4(),
            attachments=[],
            dispatcher=None,
        )
        provider = ScriptedProvider(
            [
                {
                    "text": "",
                    "tool_calls": [tool_call(name=FAKE_TOOL, arguments='{"n": 2}')],
                },
                {"text": "假工具跑完了。"},
            ]
        )

        turn = await run_turn(
            cfg, provider=provider, history=[], user_text="试试新工具", run_tool=runner
        )

        assert seen == [{"n": 2}]  # 分发到了假 handler
        assert provider.calls[0].tools is not None
        assert (
            provider.calls[0].tools[-1]["function"]["name"] == FAKE_TOOL
        )  # 清单也带上了
        assert turn.tool_calls[0]["tool"] == FAKE_TOOL
        assert turn.tool_calls[0]["status"] == "succeeded"
        assert turn.reply == "假工具跑完了。"
        # 工具内部用掉的 prompt 版本要汇总进这一轮
        assert turn.prompt_versions == {TASK_ID: SUPERVISOR_VERSION, FAKE_TASK: 7}

    @pytest.mark.asyncio
    async def test_unknown_tool_error_message_is_unchanged(
        self, tmp_path: Path
    ) -> None:
        """对外行为逐字不变：不在清单里的名字仍记 failed 并收尾。"""
        cfg = write_config(tmp_path)
        turn, provider, runner = await run(
            cfg, steps=[{"text": "", "tool_calls": [tool_call(name="no_such_tool")]}]
        )

        assert runner.calls == [] and len(provider.calls) == 1
        assert turn.tool_calls[0]["error"] == "模型要调一个不存在的工具"


# ---------- PR2 · Task 7：清单按开关下发、轮次预算按开关抬高 ----------


def write_web_config(
    tmp_path: Path, *, enabled: bool = True, fetch_enabled: bool = True
) -> AppConfig:
    """带 [web] 段的配置（其余默认）：下发的清单只由这两个开关决定。"""
    return write_config(
        tmp_path,
        "[web]\n"
        f"enabled = {str(enabled).lower()}\n"
        'search_url = "http://127.0.0.1:8080"\n'
        f"fetch_enabled = {str(fetch_enabled).lower()}\n",
    )


class TestToolsWiring:
    """PR2 · Task 7：_build_graph 收本轮清单，未知工具判定与轮次预算各按各的口径。

    D5：remaining==0 时下发改空，但「认识哪些工具」不跟着变；
    D4：[chat].max_tool_rounds 的代码默认值仍是 3，网页开启时取
        effective_max_rounds(cfg)（至少 8），显式 max_rounds 实参优先。
    """

    @pytest.mark.asyncio
    async def test_tools_disabled_matches_legacy_list(self, tmp_path: Path) -> None:
        """关闭态下发的清单与旧 TOOLS **逐项相等**：默认能力不被悄悄改动。"""
        cfg = write_web_config(tmp_path, enabled=False)
        _turn, provider, _runner = await run(cfg, steps=[{"text": "好的"}])

        assert provider.calls[0].tools == TOOLS

    @pytest.mark.asyncio
    async def test_tools_enabled_appends_web_tools(self, tmp_path: Path) -> None:
        """开启态按 tools_for(cfg) 下发：网页两件追加在老工具之后。"""
        cfg = write_web_config(tmp_path)
        _turn, provider, _runner = await run(cfg, steps=[{"text": "好的"}])

        names = [item["function"]["name"] for item in provider.calls[0].tools or []]
        assert names == [
            item["function"]["name"] for item in chat_tools.tools_for(cfg)
        ]
        assert names[: len(TOOLS)] == [item["function"]["name"] for item in TOOLS]
        assert names[len(TOOLS):] == [
            chat_tools.TOOL_SEARCH_HOTSPOTS,
            chat_tools.TOOL_FETCH_URL,
        ]

    @pytest.mark.asyncio
    async def test_tools_are_omitted_when_rounds_exhausted(self, tmp_path: Path) -> None:
        """到上限的最后一次调用不带工具（网页开启也不带）——逼模型收尾。"""
        cfg = write_web_config(tmp_path)
        _turn, provider, runner = await run(
            cfg,
            max_rounds=1,
            steps=[
                {"text": "", "tool_calls": [tool_call()]},
                {"text": "按现有结果收尾"},
            ],
        )

        assert len(runner.calls) == 1
        assert provider.calls[1].tools is None

    @pytest.mark.asyncio
    async def test_hallucinated_disabled_tool_falls_into_unknown_branch(
        self, tmp_path: Path
    ) -> None:
        """停用的工具被模型点名：走既有「未知工具」分支，落 failed 记录并收尾。"""
        cfg = write_web_config(tmp_path, enabled=False)
        turn, provider, runner = await run(
            cfg,
            steps=[
                {
                    "text": "",
                    "tool_calls": [
                        tool_call(
                            name=chat_tools.TOOL_SEARCH_HOTSPOTS,
                            arguments='{"query": "夜跑"}',
                        )
                    ],
                },
                {"text": "这个能力还没开，先用现有素材吧。"},
            ],
        )

        assert runner.calls == []  # 没下发的工具不会被真的执行
        assert [item["tool"] for item in turn.tool_calls] == [
            chat_tools.TOOL_SEARCH_HOTSPOTS
        ]
        assert turn.tool_calls[0]["status"] == "failed"
        assert turn.tool_calls[0]["error"] == "模型要调一个不存在的工具"
        assert len(provider.calls) == 1  # 不再回头问模型
        assert turn.reply  # 但必须给用户一句话，不能空回复

    @pytest.mark.asyncio
    async def test_registered_set_stays_full_when_rounds_exhausted(
        self, tmp_path: Path
    ) -> None:
        """D5：到上限后「认识哪些工具」不跟着下发改空，已知工具不被误判成未知。"""
        cfg = write_web_config(tmp_path)
        provider = ScriptedProvider(
            [
                {"text": "", "tool_calls": [tool_call(call_id="call-a")]},
                # 这一跳 remaining==0：工具已经不给了，模型却还是点了已知工具
                {"text": "", "tool_calls": [tool_call(call_id="call-b")]},
                {"text": "按现有结果收尾"},
            ]
        )
        turn, _provider, runner = await run(cfg, provider=provider, max_rounds=1)

        assert len(runner.calls) == 1  # 到上限就不再执行
        # 只有真实执行过的那条记录：若拿被裁空的清单判定，这里会多一条 failed
        assert [(item["tool"], item["status"]) for item in turn.tool_calls] == [
            (TOOL_RUN_ANALYSIS, "succeeded")
        ]

    @pytest.mark.asyncio
    async def test_disabled_web_keeps_the_configured_round_budget(
        self, tmp_path: Path
    ) -> None:
        """D4 的另一半：网页**关闭**且不传实参时预算严格等于 [chat].max_tool_rounds（3）。"""
        cfg = write_web_config(tmp_path, enabled=False)
        assert chat_tools.effective_max_rounds(cfg) == 3  # 接线要跟随这个值，不能无条件 8
        provider = ScriptedProvider(
            [{"text": "", "tool_calls": [tool_call()]} for _ in range(4)]
            + [{"text": "按现有结果收尾"}]
        )
        turn, provider, runner = await run(cfg, provider=provider)

        assert len(runner.calls) == 3  # 第 4 次不再执行工具
        assert provider.calls[3].tools is None  # 第 4 次调用已经不带工具
        assert len(provider.calls) == 4
        assert turn.reply  # 到上限也要给用户一段话

    @pytest.mark.asyncio
    async def test_web_enabled_raises_the_round_budget(self, tmp_path: Path) -> None:
        """D4：网页开启时轮次预算取 effective_max_rounds(cfg)（默认 3 → 至少 8）。"""
        cfg = write_web_config(tmp_path)
        provider = ScriptedProvider(
            [{"text": "", "tool_calls": [tool_call()]} for _ in range(20)]
        )
        _turn, provider, runner = await run(cfg, provider=provider)

        assert len(runner.calls) == chat_tools.WEB_MAX_TOOL_ROUNDS
        assert provider.calls[chat_tools.WEB_MAX_TOOL_ROUNDS].tools is None

    @pytest.mark.asyncio
    async def test_explicit_max_rounds_wins_over_web_default(
        self, tmp_path: Path
    ) -> None:
        """D4：显式传入的 max_rounds 优先于 effective_max_rounds(cfg)。"""
        cfg = write_web_config(tmp_path)  # 网页开启 → 不传实参时会到 8 轮
        _turn, provider, runner = await run(
            cfg,
            max_rounds=2,
            steps=[
                {"text": "", "tool_calls": [tool_call()]},
                {"text": "", "tool_calls": [tool_call(call_id="call-2")]},
                {"text": "收尾"},
            ],
        )

        assert len(runner.calls) == 2
        assert provider.calls[2].tools is None
        assert len(provider.calls) == 3


class TestStructuredWorkerProgress:
    """R3（OBS-05）：worker 的结构化阶段投影原样穿透工具通道，且分层表达身份。"""

    @pytest.mark.asyncio
    async def test_worker_update_passes_through_with_layered_attempts(
        self, tmp_path: Path
    ):
        cfg = write_config(tmp_path)
        events = Events()

        class WorkerTools(FakeTools):
            async def __call__(
                self, name: str, args: dict[str, Any], *, on_progress=None
            ) -> ToolOutcome:
                assert on_progress is not None
                # worker 侧结构化投影：真实状态 / 源尝试号 / 重试 / 身份 / 终态耗时
                await on_progress(
                    "拆解",
                    0,
                    0,
                    update={
                        "stage_key": "clue",
                        "label": "拆解",
                        "status": "failed",
                        "attempt": 1,
                        "retries": 2,
                        "error_category": "llm_error",
                        "run_id": "run-9",
                        "job_id": "job-9",
                    },
                )
                await on_progress(
                    "分析中",
                    30,
                    100,
                    update={
                        "stage_key": "clue",
                        "label": "拆解",
                        "status": "running",
                        "attempt": 2,
                        "retries": 2,
                        "error_category": "",
                        "run_id": "run-9",
                        "job_id": "job-9",
                    },
                )
                await on_progress(
                    "报告",
                    100,
                    100,
                    update={
                        "stage_key": "report",
                        "label": "报告",
                        "status": "succeeded",
                        "attempt": 1,
                        "retries": 0,
                        "run_id": "run-9",
                        "job_id": "job-9",
                        "latency_ms": 7,
                    },
                )
                # 工具内部的旧式标签进度（不带 update）：attempt 退回工具第几次调用
                await on_progress("解析图片", 0, 0)
                return self.outcome

        tools = WorkerTools()
        await run(
            cfg,
            events=events,
            tools=tools,
            steps=[{"text": "", "tool_calls": [tool_call()]}, {"text": "好了"}],
        )

        progress_events = [
            payload for name, payload in events.items if name == "tool_progress"
        ]
        failed = progress_events[0]
        assert (
            failed["stage_key"],
            failed["status"],
            failed["attempt"],
            failed["retries"],
            failed["error_category"],
        ) == ("clue", "failed", 1, 2, "llm_error")
        assert failed["tool_attempt"] == 1  # 工具第几次调用（分层，不代用）
        assert failed["run_id"] == "run-9" and failed["job_id"] == "job-9"
        retried = progress_events[1]
        assert retried["status"] == "running" and retried["attempt"] == 2
        # R5（E7OBS2-03）：结构化终态耗时端到端透传（此前 body 漏字段，持久化恒为 0）
        report = progress_events[2]
        assert (report["stage_key"], report["status"], report["latency_ms"]) == (
            "report",
            "succeeded",
            7,
        )
        plain = progress_events[3]
        assert plain["stage_key"] == "image_parse"  # 旧式标签进度仍映射阶段键
        assert plain["attempt"] == 1 and plain["status"] == "running"
        assert plain["run_id"] is None  # 无 worker 身份就不虚构
        # tool_finished 带上 worker 来源事实：消费侧不得用工具次数覆盖阶段重试号 / 状态
        finished = [
            payload for name, payload in events.items if name == "tool_finished"
        ][0]
        assert finished["structured"] is True
        assert (
            finished["worker_status"] == "succeeded" and finished["worker_attempt"] == 1
        )
        assert finished["worker_retries"] == 0
        assert finished["worker_latency"] == 7  # 最后已知的 worker 终态耗时


class TestFailureLedgerAndUsage:
    """R3（OBS-10 / OBS-07）：失败保留已知账目与引用；完整性按调用集合上卷。"""

    @pytest.mark.asyncio
    async def test_partial_failure_keeps_known_usage_and_references(
        self, tmp_path: Path
    ):
        """模型成功 → 工具成功 → 第二次模型失败：前两段消费与引用不丢，未知单列。"""
        cfg = write_config(tmp_path)
        second = asyncio.Event()

        class Flaky(ScriptedProvider):
            """第一次调用走剧本，第二次直接炸（模拟后续模型调用失败）。"""

            async def complete_with_tools(self, call, on_delta=None):
                if second.is_set():
                    raise RuntimeError("第二次模型调用失败")
                second.set()
                return await super().complete_with_tools(call, on_delta)

        tools = FakeTools(
            ToolOutcome(
                status="succeeded",
                payload={
                    "run_id": "r-1",
                    "candidates": 5,
                    "cost_cny": 0.05,
                    "prompt_tokens": 700,
                    "completion_tokens": 300,
                    "usage_status": "actual",
                },
                steps=[{"stage": "完成", "done": 100, "total": 100, "at": "t"}],
            )
        )
        turn, _provider, _runner = await run(
            cfg,
            events=Events(),
            tools=tools,
            steps=[{"text": "", "tool_calls": [tool_call()]}, {"text": "好了"}],
            provider=Flaky(
                [{"text": "", "tool_calls": [tool_call()]}, {"text": "不会走到这里"}]
            ),
        )

        assert turn.status == "failed" and "RuntimeError" in turn.error
        assert "第二次模型调用失败" not in turn.error
        # 已知消费与引用：第一次 supervisor（10/5）+ 那次工具（700/300）都在
        assert turn.cost_cny == pytest.approx(0.001 + 0.05)
        assert turn.prompt_tokens == 710 and turn.completion_tokens == 305
        assert turn.run_id == "r-1"
        assert [entry["tool"] for entry in turn.tool_calls] == [TOOL_RUN_ANALYSIS]
        assert turn.prompt_versions.get(TASK_ID) == SUPERVISOR_VERSION
        # 失败的那次调用用量未知：单列（已知两次 + 失败那次 1 次）
        assert turn.usage_complete is False
        assert turn.usage_unknown_calls == 1

    @pytest.mark.asyncio
    async def test_failed_tool_counts_as_unknown_not_zero(self, tmp_path: Path):
        """工具失败（调用已发生、无用量）：完整性为假、未知 +1，不填零冒充准确。"""
        cfg = write_config(tmp_path)
        tools = FakeTools(
            outcome=ToolOutcome(
                status="failed",
                payload={"run_id": "r-2", "usage_status": "unknown"},
                error="分析失败",
            )
        )
        turn, _provider, _runner = await run(
            cfg,
            events=Events(),
            tools=tools,
            steps=[
                {"text": "", "tool_calls": [tool_call()]},
                {"text": "分析失败了，我说明一下原因"},
            ],
        )

        assert turn.status == "succeeded"  # 工具失败不炸整轮（S7.4 语义）
        assert turn.cost_cny == pytest.approx(0.002)  # 只有 supervisor 那次有已知消费
        assert turn.usage_complete is False
        assert turn.usage_unknown_calls == 1

    @pytest.mark.asyncio
    async def test_successful_turn_with_known_usage_is_complete(self, tmp_path: Path):
        cfg = write_config(tmp_path)
        tools = FakeTools(
            ToolOutcome(
                status="succeeded",
                payload={
                    "run_id": "r-1",
                    "candidates": 5,
                    "cost_cny": 0.05,
                    "prompt_tokens": 700,
                    "completion_tokens": 300,
                    "usage_status": "actual",
                },
                steps=[{"stage": "完成", "done": 100, "total": 100, "at": "t"}],
            )
        )
        turn, _provider, _runner = await run(
            cfg,
            events=Events(),
            tools=tools,
            steps=[{"text": "", "tool_calls": [tool_call()]}, {"text": "好了"}],
        )

        assert turn.status == "succeeded"
        assert turn.usage_complete is True and turn.usage_unknown_calls == 0

    @pytest.mark.asyncio
    async def test_zero_usage_actual_and_cached_are_not_unknown(self, tmp_path: Path):
        """R5（E7OBS2-04）：actual 零值（供应商零用量 / 零单价）与缓存命中都不算未知。"""
        cfg = write_config(tmp_path)
        zero_actual = FakeTools(
            ToolOutcome(
                status="succeeded",
                payload={
                    "run_id": "r-1",
                    "cost_cny": 0.0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "usage_status": "actual",
                },
                steps=[],
            )
        )
        turn, _p, _r = await run(
            cfg,
            events=Events(),
            tools=zero_actual,
            steps=[{"text": "", "tool_calls": [tool_call()]}, {"text": "好了"}],
        )
        assert turn.usage_complete is True and turn.usage_unknown_calls == 0

        cached = FakeTools(
            ToolOutcome(
                status="succeeded",
                payload={"run_id": "r-1", "usage_status": "cached"},
                steps=[],
            )
        )
        turn, _p, _r = await run(
            cfg,
            events=Events(),
            tools=cached,
            steps=[{"text": "", "tool_calls": [tool_call()]}, {"text": "好了"}],
        )
        assert turn.usage_complete is True and turn.usage_unknown_calls == 0

    @pytest.mark.asyncio
    async def test_numbered_record_without_source_counts_unknown(self, tmp_path: Path):
        """R5（E7OBS2-04）：有数字但**没标来源**的记录按未知单列，不冒充完整。"""
        cfg = write_config(tmp_path)
        tools = FakeTools(
            ToolOutcome(
                status="succeeded",
                payload={
                    "run_id": "r-1",
                    "cost_cny": 0.02,
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                },
                steps=[],
            )
        )
        turn, _p, _r = await run(
            cfg,
            events=Events(),
            tools=tools,
            steps=[{"text": "", "tool_calls": [tool_call()]}, {"text": "好了"}],
        )

        assert turn.usage_complete is False and turn.usage_unknown_calls == 1

    @pytest.mark.asyncio
    async def test_non_billable_tool_does_not_affect_completeness(self, tmp_path: Path):
        """查库 / 读运行这类"无调用无数字"的工具不参与核算。"""
        cfg = write_config(tmp_path)
        tools = FakeTools(
            ToolOutcome(
                status="succeeded", payload={"run_id": "r-1", "count": 3}, steps=[]
            )
        )
        turn, _p, _r = await run(
            cfg,
            events=Events(),
            tools=tools,
            steps=[{"text": "", "tool_calls": [tool_call()]}, {"text": "好了"}],
        )

        assert turn.usage_complete is True and turn.usage_unknown_calls == 0


class TestCancellationConsistency:
    """R5（E7OBS2-07）：真实 workflow + runner 的 Task.cancel 回归。

    取消不是失败：当前执行投影 interrupted / cancelled（不再是先发 failed 再被外层改成
    interrupted 的矛盾路径）；取消继续传播；工具等待中的取消如实缺席、不伪造终态。
    """

    @pytest.mark.asyncio
    async def test_cancel_during_supervise_is_interrupted_not_failed(
        self, tmp_path: Path
    ):
        cfg = write_config(tmp_path)
        events = Events()
        entered = asyncio.Event()

        class HangingProvider(ScriptedProvider):
            async def complete_with_tools(self, call, on_delta=None):
                entered.set()
                await asyncio.sleep(3600)

        task = asyncio.create_task(
            run(cfg, events=events, provider=HangingProvider([]))
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():  # pragma: no cover - 兜底清理
                task.cancel()

        stages = [
            p
            for n, p in events.items
            if n == "stage" and p.get("stage") == "supervisor"
        ]
        assert stages, "至少应有一条 supervisor 阶段事件"
        assert stages[-1]["status"] == "interrupted"  # 不是 failed
        assert stages[-1]["error_category"] == "cancelled"
        assert all(p.get("status") != "failed" for p in stages)  # 不再先发 failed

    @pytest.mark.asyncio
    async def test_cancel_during_tool_does_not_fabricate_a_terminal(
        self, tmp_path: Path
    ):
        cfg = write_config(tmp_path)
        events = Events()
        entered = asyncio.Event()

        async def blocking_tool(_name: str, _args: dict[str, Any], *, on_progress=None):
            entered.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(
            run(
                cfg,
                events=events,
                tools=blocking_tool,
                steps=[{"text": "", "tool_calls": [tool_call()]}],
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():  # pragma: no cover - 兜底清理
                task.cancel()

        # 工具等待中取消：没有 chat.turn 级的失败事件被伪造；supervisor 已成功的那条保留
        stages = [
            p
            for n, p in events.items
            if n == "stage" and p.get("stage") == "supervisor"
        ]
        assert [p["status"] for p in stages] == ["running", "succeeded"]
        assert all(
            p.get("status") != "failed" for _n, p in events.items if _n == "stage"
        )


class TestCallIdentity:
    """E7OBS4-01 / A3：同名工具执行两次，call_id 与运行引用一一对应。"""

    @pytest.mark.asyncio
    async def test_same_tool_twice_keeps_distinct_identity(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        metas: list[dict[str, Any]] = []

        class TwoRuns(FakeTools):
            async def __call__(
                self, name: str, args: dict[str, Any], *, on_progress=None, meta=None
            ) -> ToolOutcome:
                metas.append(dict(meta or {}))
                index = len(metas)
                return ToolOutcome(
                    status="succeeded",
                    payload={"run_id": f"r-{index}", "cost_cny": 0.01 * index},
                    steps=[],
                )

        provider = ScriptedProvider(
            [
                {"tool_calls": [tool_call(call_id="call-a")]},
                {
                    "tool_calls": [
                        tool_call(call_id="call-b", arguments='{"hotspot": "晨跑"}')
                    ]
                },
                {"text": "两次都跑完了"},
            ]
        )

        turn, _provider, _runner = await run(cfg, provider=provider, tools=TwoRuns())

        assert [item["call_id"] for item in turn.tool_calls] == ["call-a", "call-b"]
        assert [item["attempt"] for item in turn.tool_calls] == [1, 2]
        assert [item["run_id"] for item in turn.tool_calls] == ["r-1", "r-2"]
        assert [item["args"]["hotspot"] for item in turn.tool_calls] == ["夜跑", "晨跑"]
        # 内部 scope 就是 call_id：不靠可变的 active_scope 反推身份
        assert [item["scope"] for item in metas] == ["call-a", "call-b"]
        assert turn.run_id == "r-2"
        # 三次 supervisor 记录（0.001×3）+ 两次工具（0.01 + 0.02）：各自一次，不双计
        assert turn.cost_cny == pytest.approx(0.033)
