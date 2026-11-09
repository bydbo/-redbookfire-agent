"""评测脚本 `scripts/eval_chat_tools.py` 的单测（Task 11：工具下发 / `--suite` / pass^k；Task 12：`--cases`）。

只测**纯逻辑与下发清单**：模型调用一律换成假的（monkeypatch `supervise`），不联网、不需要密钥、
不写真报告。真模型效果证据由 `scripts/eval_chat_tools.py --suite …` 跑出来（Task 13）。

三条硬约束在这里落成断言：
1. 关闭态（`[web].enabled=false`）下发的清单必须与旧模块常量 `TOOLS` 逐项相等——既有评测行为不变；
2. pass^k 的判定口径只收紧不放宽：`ok` = **每次都过**，`pass@1` 只作为「至少过 1 次」的参考值；
3. v2 用例集必须逐字保留 v1 的 28 条（AGENTS.md §2.3：不原地改旧用例，只加版本）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from xhs_agent.config import AppConfig, ConfigError, WebConfig
from xhs_agent.services import chat_tools
from xhs_agent.services.chat_tools import TOOL_FETCH_URL, TOOL_SEARCH_HOTSPOTS, TOOLS

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "eval_chat_tools.py"
EVALS_CHAT_DIR = Path(__file__).resolve().parents[2] / "evals" / "chat"
CASES_V1_PATH = EVALS_CHAT_DIR / "cases.json"
CASES_V2_PATH = EVALS_CHAT_DIR / "cases-v2.json"
EVALS_REPORTS_DIR = Path(__file__).resolve().parents[2] / "evals" / "reports"
SECTIONS = ("selection", "boundary", "multiturn", "revise")
# v2 新增的网页用例：selection 4 条（message → 可接受的首跳工具）+ multiturn 1 条
NEW_SELECTION_CASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("帮我找找今天有什么能蹭的热点", ("search_hotspots",)),
    ("这个链接里讲的是什么热点 https://example.com/a", ("fetch_url",)),
    ("先搜一下最近的运动热点，再挑一条跑分析", ("search_hotspots", "run_hotspot_analysis")),
    ("明天上海天气怎么样", ()),
)
NEW_MULTITURN_NAME = "网页：多轮承接（搜→分析）"
NEW_MULTISTEP_NAMES = ("多步：搜热点→跑分析", "多步：搜热点→读正文", "多步：读链接→跑分析")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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

    def test_judge_flags_a2_below_target(self, ev: Any) -> None:
        """A2 的目标是 70% / 50%，不是 prompt 回归的 100%——低于目标要明说。"""
        items = [ev.merge_runs([{"name": f"m{index}", "ok": index < 1, "steps_ok": True}])
                 for index in range(3)]

        verdict = ev.judge([], [], [], [], [], multistep=items)

        assert "A2 多步合格率 33% < 50%" in verdict

    def test_judge_flags_a2_single_step(self, ev: Any) -> None:
        items = [ev.merge_runs([{"name": f"m{index}", "ok": True, "steps_ok": index < 2}])
                 for index in range(3)]

        verdict = ev.judge([], [], [], [], [], multistep=items)

        assert "A2 多步自主 67% < 70%" in verdict


@pytest.fixture
def config_never_loaded(ev: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """让 `load_config()` 一旦被调用就炸：证明参数 guard 在加载配置**之前**就退出了。

    这样钉是因为：CI 没有密钥，真 `load_config()` 后面的密钥检查同样会 `return 1`——不钉死"没走到配置"，
    这些用例在 CI 里会**恒绿**，guard 被回滚也发现不了（本机有密钥时才红，等于靠环境运气）。
    """

    def _boom() -> Any:
        raise AssertionError("参数 guard 应该在 load_config() 之前退出")

    monkeypatch.setattr(ev, "load_config", _boom)


class TestCliGuards:
    """参数组合不成立时必须报错退出，不能落一份看着"通过"的报告。

    三条用例都传 `--out tmp_path`：万一 guard 被回滚，最坏也只是在临时目录里跑，
    绝不去写仓库的 `evals/reports/`（那里一旦被污染，diff 都难查）。
    """

    @pytest.mark.asyncio
    async def test_empty_suites_exits_without_report(self, ev: Any,
                                                     monkeypatch: pytest.MonkeyPatch,
                                                     config_never_loaded: None,
                                                     tmp_path: Path) -> None:
        # `--suite multiturn --skip-multiturn` = 一节都不跑；跑到配置之前就该退出
        monkeypatch.setattr(sys, "argv", ["eval_chat_tools.py", "--suite", "multiturn",
                                          "--skip-multiturn", "--out", str(tmp_path)])

        assert await ev.main() == 1
        assert list(tmp_path.iterdir()) == []      # 不写报告，才有机会被人发现

    @pytest.mark.asyncio
    async def test_repeat_below_one_exits(self, ev: Any,
                                          monkeypatch: pytest.MonkeyPatch,
                                          config_never_loaded: None,
                                          tmp_path: Path) -> None:
        monkeypatch.setattr(sys, "argv", ["eval_chat_tools.py", "--repeat", "0",
                                          "--out", str(tmp_path)])

        assert await ev.main() == 1
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_limit_zero_exits_instead_of_running_everything(
        self, ev: Any, monkeypatch: pytest.MonkeyPatch, config_never_loaded: None,
        tmp_path: Path,
    ) -> None:
        # 改造前 `--limit 0` 被当成「不限」跑全量（再叠 --repeat 就是全量 ×N）
        monkeypatch.setattr(sys, "argv", ["eval_chat_tools.py", "--limit", "0",
                                          "--out", str(tmp_path)])

        assert await ev.main() == 1
        assert list(tmp_path.iterdir()) == []


class TestJudgeWhenNothingRan:
    def test_judge_does_not_claim_pass_without_results(self, ev: Any) -> None:
        """四节全空 = 一条用例都没跑：不许给绿色结论，更不许说"结果都稳定"。"""
        verdict = ev.judge([], [], [], [], [], repeat=3)

        assert "**未跑任何节**" in verdict
        assert "**通过**" not in verdict
        assert "稳定" not in verdict


class TestSuiteSelection:
    def test_all_expands_to_every_section(self, ev: Any) -> None:
        assert ev.selected_suites("all") == {"selection", "boundary", "multiturn",
                                             "revise", "multistep"}

    def test_single_suite(self, ev: Any) -> None:
        assert ev.selected_suites("boundary") == {"boundary"}


class TestRender:
    @staticmethod
    def make_report(ev: Any, repeat: int,
                    cases: str = "evals/chat/cases.json") -> dict[str, Any]:
        first = {"name": "co-01", "message": "m", "expect": [], "picked": [],
                 "no_tool_ok": False, "error": "", "why": ""}
        second = {"name": "co-02", "message": "m", "expect": ["run_analysis"],
                  "picked": ["run_analysis"], "no_tool_ok": False, "error": "", "why": ""}
        selection = [ev.merge_runs([dict(first, ok=True) for _ in range(repeat)]),
                     ev.merge_runs([dict(second, ok=(index == 0)) for index in range(repeat)])]
        return {
            "date": "2026-10-30", "task": "chat_supervisor", "model": "deepseek-chat",
            "baseline_ref": "HEAD", "versions": {"baseline": "5", "candidate": "6"},
            "cases": cases, "cases_version": "chat-v1", "cost_limit": 0.25, "note": "",
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

    def test_render_names_the_case_file(self, ev: Any) -> None:
        """报告必须写明**实际用了哪个用例文件**：只写版本号，v1 / v2 两份对照不出来。"""
        markdown = ev.render_markdown(
            self.make_report(ev, repeat=1, cases="evals/chat/cases-v2.json"))

        assert "evals/chat/cases-v2.json" in markdown

    def test_render_multistep_a2_section(self, ev: Any) -> None:
        """多步节要写出 A2 门槛与达标结论，并进 pass^k 汇总表。"""
        report = self.make_report(ev, repeat=3)
        item = ev.merge_runs([
            {"name": "多步：搜热点→跑分析", "message": "m",
             "picked": ["search_hotspots", "run_hotspot_analysis"], "steps": 2,
             "steps_ok": True, "min_steps": 2, "first_ok": True,
             "grounded": ["球拍特写"], "must_contain_any": ["球拍特写"],
             "reply": "…球拍特写…", "cost_cny": 0.01, "latency_ms": 100,
             "status": "succeeded", "error": "", "notes": "", "ok": True}
            for _ in range(3)])
        report["arms"]["candidate"]["multistep"] = [item]
        report["metrics"]["multistep"] = ev.pass_metrics([item])

        markdown = ev.render_markdown(report)

        assert "## 五、多步" in markdown
        assert "A2 门槛 ≥70% → 达标" in markdown
        assert "多步（A2）" in markdown          # pass^k 汇总表里的行
        assert "search_hotspots → run_hotspot_analysis" in markdown

    def test_render_without_cases_key_still_works(self, ev: Any) -> None:
        """`cases` 是本次新加的字段：加字段之前落盘的报告 JSON 没有它，二次渲染不许 KeyError。"""
        report = self.make_report(ev, repeat=1)
        del report["cases"]

        assert "evals/chat/cases.json" in ev.render_markdown(report)

    def test_renders_the_oldest_historical_report(self, ev: Any) -> None:
        """最老的归档报告（2026-10-11）没有 `cases_version` / `cost_limit`，也没有 boundary /
        multiturn 两节——渲染器必须能读它，缺的节按「当时没跑」显示，不许 KeyError。"""
        report = read_json(EVALS_REPORTS_DIR / "chat-tools-2026-10-11.json")

        markdown = ev.render_markdown(report)

        assert "# 对话层回归评测 · 2026-10-11" in markdown
        assert "本节未运行" in markdown
        assert "成本门槛 ≤ 0.25 元/轮" in markdown      # cost_limit 缺失时回落脚本当前门槛


class TestMultistep:
    """A2 多步节：步数 + 首跳 + 依据锚点 + 成本，四条缺一不可（只数调用次数不算成功）。"""

    @staticmethod
    def fake_turn(ev: Any, picked: list[str], reply: str, *,
                  cost: float = 0.01, status: str = "succeeded") -> Any:
        return SimpleNamespace(status=status, error="", reply=reply,
                               tool_calls=[{"tool": name} for name in picked],
                               cost_cny=cost, latency_ms=100)

    @staticmethod
    def case(ev: Any, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "name": "m", "message": "帮我找热点再跑分析",
            "first_tool": frozenset({"search_hotspots"}),
            "must_contain_any": ("球拍特写",),
        }
        values.update(overrides)
        return ev.MultistepCase(**values)

    @pytest.mark.asyncio
    async def test_two_steps_with_grounding_pass(self, ev: Any,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run_turn(cfg: Any, **kwargs: Any) -> Any:
            return self.fake_turn(ev, ["search_hotspots", "run_hotspot_analysis"],
                                  "搜到「夜跑球鞋开箱」，分析首选是「球拍特写」")

        monkeypatch.setattr(ev, "run_turn", fake_run_turn)

        result = await ev.run_multistep_case(SimpleNamespace(), provider=None, case=self.case(ev))

        assert result["ok"] is True
        assert result["steps"] == 2 and result["steps_ok"] is True
        assert result["grounded"] == ["球拍特写"]

    @pytest.mark.asyncio
    async def test_single_step_fails(self, ev: Any,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run_turn(cfg: Any, **kwargs: Any) -> Any:
            return self.fake_turn(ev, ["search_hotspots"], "球拍特写")

        monkeypatch.setattr(ev, "run_turn", fake_run_turn)

        result = await ev.run_multistep_case(SimpleNamespace(), provider=None, case=self.case(ev))

        assert result["steps_ok"] is False and result["ok"] is False

    @pytest.mark.asyncio
    async def test_two_steps_without_grounded_fact_fails(self, ev: Any,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
        """调了两次工具但终稿没有工具返回里的事实 → 不算合格（§10 判定口径）。"""
        async def fake_run_turn(cfg: Any, **kwargs: Any) -> Any:
            return self.fake_turn(ev, ["search_hotspots", "run_hotspot_analysis"],
                                  "这条热点可以蹭，我已经看完了")

        monkeypatch.setattr(ev, "run_turn", fake_run_turn)

        result = await ev.run_multistep_case(SimpleNamespace(), provider=None, case=self.case(ev))

        assert result["steps_ok"] is True and result["grounded"] == []
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_wrong_first_tool_fails(self, ev: Any,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run_turn(cfg: Any, **kwargs: Any) -> Any:
            return self.fake_turn(ev, ["fetch_url", "run_hotspot_analysis"], "球拍特写")

        monkeypatch.setattr(ev, "run_turn", fake_run_turn)

        result = await ev.run_multistep_case(SimpleNamespace(), provider=None, case=self.case(ev))

        assert result["first_ok"] is False and result["ok"] is False

    @pytest.mark.asyncio
    async def test_over_cost_fails(self, ev: Any,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run_turn(cfg: Any, **kwargs: Any) -> Any:
            return self.fake_turn(ev, ["search_hotspots", "run_hotspot_analysis"],
                                  "球拍特写", cost=1.0)

        monkeypatch.setattr(ev, "run_turn", fake_run_turn)

        result = await ev.run_multistep_case(SimpleNamespace(), provider=None, case=self.case(ev))

        assert result["ok"] is False

    def test_steps_rate_requires_every_repeat(self, ev: Any) -> None:
        """重复跑时 A2 主指标按「每次都 ≥2 步」算，与 `ok` 的严格口径一致。"""
        flaky = ev.merge_runs([{"name": "a", "ok": True, "steps_ok": True},
                               {"name": "a", "ok": True, "steps_ok": False}])
        stable = ev.merge_runs([{"name": "b", "ok": True, "steps_ok": True},
                                {"name": "b", "ok": True, "steps_ok": True}])

        assert ev._steps_rate([flaky]) == 0.0
        assert ev._steps_rate([stable]) == 1.0


class TestCasesV2:
    """v2 用例集：v1 的 28 条**逐字**保留 + 网页用例 5 条 + multistep 3 条。

    （v1 文件按 AGENTS.md §2.3 冻结不动；v2 在产出归档报告前由 PR4 修正过一轮，见用例集 note。）
    """

    def test_cases_v2_version_string(self, ev: Any) -> None:
        assert ev.load_cases(CASES_V2_PATH).version == "chat-v2"

    def test_cases_v2_keeps_v1_verbatim(self) -> None:
        """v1 的四段 28 条必须逐字出现在 v2 里——靠测试守，不靠人眼比对。"""
        v1, v2 = read_json(CASES_V1_PATH), read_json(CASES_V2_PATH)

        for section in SECTIONS:
            assert v2[section][:len(v1[section])] == v1[section], section
        assert sum(len(v1[section]) for section in SECTIONS) == 28

    def test_cases_v2_adds_web_cases(self) -> None:
        v1, v2 = read_json(CASES_V1_PATH), read_json(CASES_V2_PATH)
        added = v2["selection"][len(v1["selection"]):]

        assert [(item["message"], sorted(item["expect"])) for item in added] == [
            (message, sorted(expect)) for message, expect in NEW_SELECTION_CASES]
        assert all(item["why"].strip() for item in added)      # 新增的每条都要写清目的
        # 「把刚才搜到的第一条跑一遍分析」在单轮臂不可判定，已移到多轮节（A2 首跑 0/3 的修正）
        new_turns = v2["multiturn"][len(v1["multiturn"]):]
        assert [item["name"] for item in new_turns] == [NEW_MULTITURN_NAME]
        assert len(new_turns[0]["turns"]) == len(new_turns[0]["expect_tools"]) == 2

    def test_cases_v2_multistep_definitions_are_judgeable(self) -> None:
        """A2 用例必须自带可机读判据：≥2 步、首跳白名单、依据锚点，缺一条就等于没测。"""
        multistep = read_json(CASES_V2_PATH)["multistep"]

        assert [item["name"] for item in multistep] == list(NEW_MULTISTEP_NAMES)
        for item in multistep:
            assert item["message"].strip() and item["notes"].strip(), item["name"]
            assert item["min_steps"] >= 2, item["name"]
            assert item["first_tool"], item["name"]
            assert item["must_contain_any"], item["name"]

    def test_cases_v2_expectations_only_reference_registered_tools(self) -> None:
        """用例里点的工具名必须真实存在（网页两件只有开启态才下发）。"""
        known = chat_tools.tool_names(make_cfg(enabled=True))
        payload = read_json(CASES_V2_PATH)

        for item in payload["selection"]:
            assert set(item["expect"]) <= known, item["name"]
        for item in payload["multiturn"]:
            for group in item["expect_tools"]:
                assert set(group) <= known, item["name"]
        for item in payload["multistep"]:
            assert set(item["first_tool"]) <= known, item["name"]

    def test_cases_v2_parses_into_all_five_sections(self, ev: Any) -> None:
        """五段都要非空；`main()` 的缺段 guard 只放过 v1 没有的 multistep。"""
        cases = ev.load_cases(CASES_V2_PATH)

        assert [len(cases.selection), len(cases.boundary), len(cases.multiturn),
                len(cases.revise), len(cases.multistep)] == [14, 10, 6, 3, 3]


class TestCasesFlag:
    """Task 12：`--cases` 读别的用例文件；不传仍读 v1。"""

    def test_cases_flag_defaults_to_v1(self, ev: Any) -> None:
        assert ev.resolve_cases_path(None) == CASES_V1_PATH

    def test_cases_path_is_project_root_relative(self, ev: Any) -> None:
        """相对路径按工程根解析（脚本里其它 git 操作也是这个口径）。"""
        assert ev.resolve_cases_path("evals/chat/cases-v2.json") == CASES_V2_PATH
        assert ev.resolve_cases_path(str(CASES_V2_PATH)) == CASES_V2_PATH

    def test_cases_flag_rejects_empty_path(self, ev: Any) -> None:
        """`--cases ""` 不许静默回落 v1：分不清是「要默认」还是「变量没展开」。"""
        with pytest.raises(ConfigError, match="空路径"):
            ev.resolve_cases_path("")

    @pytest.mark.asyncio
    async def test_main_rejects_empty_cases_path(self, ev: Any,
                                                 monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:
        def boom(path: Path) -> Any:
            raise AssertionError("空路径应该在读用例之前就被拒绝")

        monkeypatch.setattr(ev, "load_config", lambda: SimpleNamespace(
            llm=SimpleNamespace(resolved_key=lambda: "fake-key")))
        monkeypatch.setattr(ev, "load_cases", boom)
        monkeypatch.setattr(sys, "argv", ["eval_chat_tools.py", "--out", str(tmp_path),
                                          "--cases", ""])

        assert await ev.main() == 1
        assert list(tmp_path.iterdir()) == []


class TestLoadCasesErrors:
    """`--cases` 之后输入面变大：坏路径一律给 `[环境] …` + 退出码 1，不抛原始 OSError。"""

    def test_missing_file_becomes_config_error(self, ev: Any) -> None:
        with pytest.raises(ConfigError, match="找不到"):
            ev.load_cases(EVALS_CHAT_DIR / "nope.json")

    def test_directory_becomes_config_error(self, ev: Any) -> None:
        """指到目录时 Windows 抛 PermissionError、Linux 抛 IsADirectoryError，都是 OSError。"""
        with pytest.raises(ConfigError, match="读不了"):
            ev.load_cases(EVALS_CHAT_DIR)

    @pytest.mark.asyncio
    async def test_main_reads_the_given_cases_file(self, ev: Any,
                                                  monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
        """`--cases` 必须真被 `main()` 用上（不是加了个没人读的参数）。"""
        seen: list[Path] = []

        def fake_load_cases(path: Path = ev.CASES_PATH) -> Any:
            seen.append(path)
            return ev.ChatCases()       # 四段全空 → 缺段 guard 退出，不会去调模型

        monkeypatch.setattr(ev, "load_config", lambda: SimpleNamespace(
            llm=SimpleNamespace(resolved_key=lambda: "fake-key")))
        monkeypatch.setattr(ev, "load_cases", fake_load_cases)
        monkeypatch.setattr(sys, "argv", ["eval_chat_tools.py", "--out", str(tmp_path),
                                          "--cases", "evals/chat/cases-v2.json"])

        assert await ev.main() == 1
        assert seen == [CASES_V2_PATH]
        assert list(tmp_path.iterdir()) == []
