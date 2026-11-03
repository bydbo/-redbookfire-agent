"""评测脚本 `scripts/eval_chat_tools.py` 的单测（Task 11：工具下发 / `--suite` / pass^k）。

只测**纯逻辑与下发清单**：模型调用一律换成假的（monkeypatch `supervise`），不联网、不需要密钥、
不写真报告。真模型效果证据由 `scripts/eval_chat_tools.py --suite …` 跑出来（Task 13）。

两条硬约束在这里落成断言：
1. 关闭态（`[web].enabled=false`）下发的清单必须与旧模块常量 `TOOLS` 逐项相等——既有评测行为不变；
2. pass^k 的判定口径只收紧不放宽：`ok` = **每次都过**，`pass@1` 只作为「至少过 1 次」的参考值。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from xhs_agent.config import AppConfig, WebConfig
from xhs_agent.services import chat_tools
from xhs_agent.services.chat_tools import TOOL_FETCH_URL, TOOL_SEARCH_HOTSPOTS, TOOLS

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "eval_chat_tools.py"


@pytest.fixture(scope="module")
def ev() -> Any:
    """按路径加载评测脚本（`if __name__ == "__main__"` 守护，导入无副作用）。"""
    spec = importlib.util.spec_from_file_location("eval_chat_tools_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass + `from __future__ import annotations` 需要模块在 sys.modules 里才能解析注解
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_cfg(**web: Any) -> AppConfig:
    """默认 AppConfig + 指定的 `[web]` 覆盖。"""
    cfg = AppConfig()
    cfg.web = WebConfig(**web)
    return cfg


def names_of(tools: list[dict[str, Any]]) -> list[str]:
    return [str(item["function"]["name"]) for item in tools]


class FakeTurn:
    """假的 supervisor 返回值：不调模型，也不点工具。"""

    def __init__(self) -> None:
        self.tool_calls: list[dict[str, Any]] = []
        self.text = ""


def patch_supervise(ev: Any, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """把脚本里的 `supervise` 换成记账假实现，返回它记下的 `tools=` 实参。"""
    calls: list[Any] = []

    async def fake_supervise(provider: Any, messages: Any, *, tools: Any = None,
                             stream: bool = False) -> FakeTurn:
        calls.append(tools)
        return FakeTurn()

    monkeypatch.setattr(ev, "supervise", fake_supervise)
    return calls


class TestDispatchedTools:
    @pytest.mark.asyncio
    async def test_tools_come_from_tools_for(self, ev: Any,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
        """选择臂下发的清单 = `chat_tools.tools_for(cfg)`；关闭态下与旧 `TOOLS` 逐项相等。"""
        calls = patch_supervise(ev, monkeypatch)
        cfg = make_cfg()
        case = ev.SelectionCase(name="co-01", message="帮我做这条热点", expect=frozenset())

        await ev.run_selection_case(cfg, provider=None, case=case)

        assert calls == [chat_tools.tools_for(cfg)]
        assert calls[0] == list(TOOLS)

    @pytest.mark.asyncio
    async def test_boundary_tools_follow_web_switch(self, ev: Any,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
        """开启态下越界那节也要跟着开关走：多出网页两件，而不是永远发老常量。"""
        calls = patch_supervise(ev, monkeypatch)
        cfg = make_cfg(enabled=True)
        case = ev.BoundaryCase(name="bd-01", category="compliance_red_line", message="帮我编点东西")

        await ev.run_boundary_case(cfg, provider=None, case=case)

        assert calls == [chat_tools.tools_for(cfg)]
        assert names_of(calls[0]) == names_of(list(TOOLS)) + [TOOL_SEARCH_HOTSPOTS, TOOL_FETCH_URL]

    @pytest.mark.asyncio
    async def test_dispatch_reads_live_tools_for(self, ev: Any,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
        """钉的是「现算 `tools_for(cfg)`」这件事，不是「恰好等于一份常量」。"""
        sentinel = [{"type": "function", "function": {"name": "sentinel_tool"}}]
        monkeypatch.setattr(chat_tools, "tools_for", lambda cfg=None: [dict(item) for item in sentinel])
        calls = patch_supervise(ev, monkeypatch)
        case = ev.SelectionCase(name="co-01", message="帮我做这条热点", expect=frozenset())

        await ev.run_selection_case(make_cfg(), provider=None, case=case)

        assert calls == [sentinel]


class TestPassAtK:
    def test_pass_at_k_computation(self, ev: Any) -> None:
        """3 条用例各跑 3 次、每条只过其中 2 次以内：pass@1 = 2/3，pass@3 = 0。"""
        per_case = [[True, True, False], [False, False, True], [False, False, False]]
        cases = [ev.merge_runs([{"name": f"c{index}", "ok": flag} for flag in flags])
                 for index, flags in enumerate(per_case)]

        metrics = ev.pass_metrics(cases)

        assert metrics["cases"] == 3
        assert metrics["pass_at_1"] == round(2 / 3, 4)
        assert metrics["pass_at_k"] == 0.0
        assert sorted(metrics["unstable"]) == ["c0", "c1"]
        # 单条用例口径：3 次里过 2 次 → 至少过一次 = True，每次都过 = False
        assert cases[0]["pass_at_1"] is True and cases[0]["pass_at_k"] is False
        assert cases[0]["ok"] is False
        assert cases[0]["successes"] == 2 and cases[0]["repeat"] == 3
        assert [run["ok"] for run in cases[0]["runs"]] == [True, True, False]

    def test_pass_at_k_all_success(self, ev: Any) -> None:
        """3 次全成：pass@1 与 pass@3 都是 1.0。"""
        cases = [ev.merge_runs([{"name": "c0", "ok": True} for _ in range(3)])]

        metrics = ev.pass_metrics(cases)

        assert metrics["pass_at_1"] == 1.0
        assert metrics["pass_at_k"] == 1.0
        assert metrics["unstable"] == []
        assert cases[0]["ok"] is True and cases[0]["successes"] == 3

    def test_repeat_one_matches_legacy_rate(self, ev: Any) -> None:
        """`--repeat 1`：两个指标都等于改造前的通过率（历史报告可以对比）。"""
        cases = [ev.merge_runs([{"name": name, "ok": ok}])
                 for name, ok in (("a", True), ("b", False))]

        metrics = ev.pass_metrics(cases)

        assert metrics["pass_at_1"] == metrics["pass_at_k"] == 0.5
        assert cases[0]["ok"] is True and cases[0]["repeat"] == 1

    def test_empty_runs_are_not_a_pass(self, ev: Any) -> None:
        """0 次运行不算过：不给出 0/0 的假通过。"""
        merged = ev.merge_runs([])

        assert merged["ok"] is False
        assert merged["pass_at_1"] is False and merged["pass_at_k"] is False
        assert merged["repeat"] == 0 and merged["runs"] == []

    @pytest.mark.asyncio
    async def test_run_section_repeats_each_case(self, ev: Any) -> None:
        """一节用例的循环：同一条跑 `repeat` 次再压成一条。"""
        answers = iter([True, False, True])

        async def run_once(case: str) -> dict[str, Any]:
            return {"name": case, "ok": next(answers)}

        results = await ev.run_section(["c0"], run_once, 3)

        assert len(results) == 1
        assert results[0]["repeat"] == 3 and results[0]["successes"] == 2
        assert results[0]["ok"] is False and results[0]["pass_at_1"] is True


class TestJudge:
    def test_judge_flags_unstable_cases(self, ev: Any) -> None:
        """重复跑时过时不过 → 结论里必须点名，不能只看某一次算过就算了。"""
        merged = ev.merge_runs([{"name": "co-01", "ok": True}, {"name": "co-01", "ok": False}])

        verdict = ev.judge([], [merged], [], [], [], repeat=2)

        assert "重复跑结果不稳定" in verdict
        assert "co-01" in verdict

    def test_judge_ignores_selection_when_not_run(self, ev: Any) -> None:
        """`--suite boundary` 没跑选择臂：不许把它算成「通过率 0%」。"""
        boundary = [ev.merge_runs([{"name": "bd-01", "ok": True}])]

        verdict = ev.judge([], [], boundary, [], [], repeat=1)

        assert "工具选择" not in verdict
        assert "**通过**" in verdict


class TestCliGuards:
    """参数组合不成立时必须报错退出，不能落一份看着"通过"的报告。"""

    @pytest.mark.asyncio
    async def test_empty_suites_exits_without_report(self, ev: Any,
                                                     monkeypatch: pytest.MonkeyPatch,
                                                     tmp_path: Path) -> None:
        # `--suite multiturn --skip-multiturn` = 一节都不跑；跑之前就该退出
        monkeypatch.setattr(sys, "argv", ["eval_chat_tools.py", "--suite", "multiturn",
                                          "--skip-multiturn", "--out", str(tmp_path)])

        assert await ev.main() == 1
        assert list(tmp_path.iterdir()) == []      # 不写报告，才有机会被人发现

    @pytest.mark.asyncio
    async def test_repeat_below_one_exits(self, ev: Any,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["eval_chat_tools.py", "--repeat", "0"])

        assert await ev.main() == 1

    @pytest.mark.asyncio
    async def test_limit_zero_exits_instead_of_running_everything(self, ev: Any,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        # 改造前 `--limit 0` 被当成「不限」跑全量（再叠 --repeat 就是全量 ×N）
        monkeypatch.setattr(sys, "argv", ["eval_chat_tools.py", "--limit", "0"])

        assert await ev.main() == 1


class TestJudgeWhenNothingRan:
    def test_judge_does_not_claim_pass_without_results(self, ev: Any) -> None:
        """四节全空 = 一条用例都没跑：不许给绿色结论，更不许说"结果都稳定"。"""
        verdict = ev.judge([], [], [], [], [], repeat=3)

        assert "**未跑任何节**" in verdict
        assert "**通过**" not in verdict
        assert "稳定" not in verdict


class TestSuiteSelection:
    def test_all_expands_to_every_section(self, ev: Any) -> None:
        assert ev.selected_suites("all") == {"selection", "boundary", "multiturn", "revise"}

    def test_single_suite(self, ev: Any) -> None:
        assert ev.selected_suites("boundary") == {"boundary"}


class TestRender:
    @staticmethod
    def make_report(ev: Any, repeat: int) -> dict[str, Any]:
        first = {"name": "co-01", "message": "m", "expect": [], "picked": [],
                 "no_tool_ok": False, "error": "", "why": ""}
        second = {"name": "co-02", "message": "m", "expect": ["run_analysis"],
                  "picked": ["run_analysis"], "no_tool_ok": False, "error": "", "why": ""}
        selection = [ev.merge_runs([dict(first, ok=True) for _ in range(repeat)]),
                     ev.merge_runs([dict(second, ok=(index == 0)) for index in range(repeat)])]
        return {
            "date": "2026-10-30", "task": "chat_supervisor", "model": "deepseek-chat",
            "baseline_ref": "HEAD", "versions": {"baseline": "5", "candidate": "6"},
            "cases_version": "chat-v1", "cost_limit": 0.25, "note": "",
            "suite": "all", "repeat": repeat,
            "metrics": {"selection": ev.pass_metrics(selection)},
            "arms": {"baseline": {"selection": selection},
                     "candidate": {"selection": selection, "boundary": [],
                                   "multiturn": [], "revise": []}},
            "verdict": "**通过**：工具选择 100%。",
        }

    def test_render_reports_pass_at_k(self, ev: Any) -> None:
        markdown = ev.render_markdown(self.make_report(ev, repeat=3))

        assert "pass@3" in markdown
        assert "1/3" in markdown          # co-02 三次里只过了一次
        assert "co-02" in markdown

    def test_render_single_repeat_stays_legacy(self, ev: Any) -> None:
        markdown = ev.render_markdown(self.make_report(ev, repeat=1))

        assert "pass@1" in markdown
        assert "等价于改造前的通过率" in markdown
