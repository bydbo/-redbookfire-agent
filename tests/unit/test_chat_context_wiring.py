"""对话侧接入（S7.24 / S7.26）：状态注入、成组历史与请求前预算拦截。

不联网不连库：provider / 工具执行器全部注入。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from xhs_agent.agents.supervisor import TASK_ID, build_messages, initial_messages
from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.budget import BudgetPolicy
from xhs_agent.services.chat_turns import ContextPrep
from xhs_agent.tools import llm
from xhs_agent.tools import prompt as prompt_tool
from xhs_agent.tools.llm import LLMResult
from xhs_agent.workflows.chat import run_turn


def write_config(tmp_path: Path, body: str = "") -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\nruns_dir = "{(tmp_path / "runs").as_posix()}"\n' + body,
        encoding="utf-8",
    )
    return load_config(str(path))


class ScriptedProvider(llm.BaseProvider):
    name = "scripted"

    def __init__(self, text: str = "好的") -> None:
        super().__init__(model="scripted-1")
        self.text = text
        self.calls = 0

    async def complete(self, call: llm.LLMCall) -> LLMResult:  # pragma: no cover
        raise AssertionError("对话用例不该走 complete()")

    async def complete_with_tools(self, call: llm.LLMCall, on_delta=None) -> LLMResult:
        self.calls += 1
        if on_delta is not None:
            await on_delta(self.text)
        return LLMResult(
            text=self.text, provider=self.name, model=self.model,
            prompt_tokens=10, completion_tokens=5, cost_cny=0.001,
            usage_status="actual",
        )


class TestBuildMessages:
    def test_state_text_sits_after_fixed_prefix(self) -> None:
        rendered = prompt_tool.render(TASK_ID, capabilities=prompt_tool.render_capabilities())
        messages = build_messages(
            rendered, [], "改成 100 字", state_text="[当前状态] 控制在 300 字以内"
        )
        assert [item["role"] for item in messages[:3]] == ["system", "user", "system"]
        assert messages[2]["content"].startswith("[当前状态]")

    def test_grouped_history_keeps_full_text(self) -> None:
        rendered = prompt_tool.render(TASK_ID, capabilities=prompt_tool.render_capabilities())
        history = [
            {"role": "user", "content": "旧" * 100},
            {"role": "assistant", "content": "旧答" * 100},
            {"role": "user", "content": "新"},
            {"role": "assistant", "content": "新答"},
        ]
        messages = build_messages(
            rendered, history, "本轮", grouped=True, token_budget=40
        )
        contents = [item["content"] for item in messages]
        assert "新" in contents and "新答" in contents
        assert not any("…" in str(item) for item in contents if item)

    def test_off_mode_keeps_legacy_digest(self) -> None:
        rendered = prompt_tool.render(TASK_ID, capabilities=prompt_tool.render_capabilities())
        history = [{"role": "user", "content": "旧" * 100}, {"role": "user", "content": "新"}]
        messages = build_messages(rendered, history, "本轮", keep_messages=1,
                                  token_budget=100_000)
        assert messages[2]["content"].endswith("…")

    def test_dropped_groups_are_disclosed(self) -> None:
        rendered = prompt_tool.render(TASK_ID, capabilities=prompt_tool.render_capabilities())
        history = [
            {"role": "user", "content": "旧" * 200},
            {"role": "assistant", "content": "旧答" * 200},
            {"role": "user", "content": "新"},
        ]
        messages = build_messages(rendered, history, "本轮", grouped=True, token_budget=30)
        assert "没装进上下文" in messages[-1]["content"]


class TestBudgetEnforcement:
    @pytest.mark.asyncio
    async def test_enforce_stops_before_any_request(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        provider = ScriptedProvider()
        turn = await run_turn(
            cfg,
            provider=provider,
            history=[],
            user_text="帮我写一版文案",
            run_tool=_unused_tool,
            budget_policy=BudgetPolicy(
                application_limit=200, model_window=None, mode="enforce",
                output_tokens=16000, protected=("本轮问题",),
            ),
        )
        assert provider.calls == 0, "超限时不应发出模型请求"
        assert turn.status == "failed"
        assert turn.error_category == "required_context_overflow"

    @pytest.mark.asyncio
    async def test_shadow_only_records(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        provider = ScriptedProvider("好的")
        decisions: list[Any] = []

        async def on_budget(decision: Any) -> None:
            decisions.append(decision)

        turn = await run_turn(
            cfg,
            provider=provider,
            history=[],
            user_text="帮我写一版文案",
            run_tool=_unused_tool,
            budget_policy=BudgetPolicy(
                application_limit=200, model_window=None, mode="shadow",
                output_tokens=16000,
            ),
            on_budget=on_budget,
        )
        assert provider.calls == 1
        assert turn.status == "succeeded"
        assert decisions and decisions[0].allowed is False
        assert decisions[0].mode == "shadow"

    @pytest.mark.asyncio
    async def test_off_mode_does_not_check(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        provider = ScriptedProvider("好的")
        turn = await run_turn(
            cfg, provider=provider, history=[], user_text="你好", run_tool=_unused_tool,
            budget_policy=BudgetPolicy(application_limit=1, mode="off", output_tokens=16000),
        )
        assert provider.calls == 1 and turn.status == "succeeded"


class TestContextPrep:
    def test_off_mode_returns_empty_prep(self) -> None:
        prep = ContextPrep()
        assert prep.enabled is False and prep.state_text == "" and prep.grouped is False


async def _unused_tool(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - 不会被调用
    raise AssertionError("这一轮不该调用工具")


def test_initial_messages_carries_state_and_grouped_history() -> None:
    _, messages = initial_messages(
        [{"role": "user", "content": "旧"}, {"role": "assistant", "content": "旧答"}],
        "新问题",
        grouped=True,
        state_text="[当前状态] 目标：蹭热点",
    )
    assert messages[2]["content"].startswith("[当前状态]")
    assert json.dumps(messages[-1], ensure_ascii=False).endswith('"}')
