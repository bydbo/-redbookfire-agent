"""Prompt 回归脚本的纯函数单测（S3.10）：按路径加载脚本模块，不连库、不联网、不调模型。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "eval_prompts.py"


@pytest.fixture(scope="module")
def ev():
    """按路径加载回归脚本（有 `__main__` 守护，导入无副作用）。"""
    spec = importlib.util.spec_from_file_location("eval_prompts_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_case(case_id: str = "case-01", *, must=(("topic", "羽毛球"),),
              allowed=(("scene", "球场"),), forbidden=(), must_hit=("运动/球场-挥拍.mp4",),
              acceptable=(), must_not=()) -> dict:
    return {
        "id": case_id,
        "version": "v1",
        "scene": "明星运动",
        "hotspot": "某明星打羽毛球被拍",
        "clue_expectations": {
            "must": [{"type": etype, "value": value} for etype, value in must],
            "allowed": [{"type": etype, "value": value} for etype, value in allowed],
            "forbidden": [{"type": etype, "value": value, "reason": "编造"}
                          for etype, value in forbidden],
        },
        "match_expectations": {
            "must_hit": list(must_hit),
            "acceptable": list(acceptable),
            "must_not": list(must_not),
        },
        "notes": "用例单测",
    }


def make_clue(*elements: tuple[str, str]) -> dict:
    return {"hotspot_raw": "某明星打羽毛球被拍", "hotspot_key": "k-1",
            "elements": [{"type": etype, "value": value} for etype, value in elements]}


def make_candidate(path: str = "运动/球场-挥拍.mp4", *, reasons=None, usage: str = "放开头 3 秒",
                   sources=("literal", "vector")) -> dict:
    return {
        "material_id": "m-1",
        "rank": 1,
        "score": 0.8,
        "recall_sources": list(sources),
        "reasons": ["命中主题"] if reasons is None else reasons,
        "usage": usage,
        "hits": [],
        "missing": [],
        "material": {"id": "m-1", "path": path, "type": "video", "title": "球场热身"},
    }


def make_draft() -> dict:
    return {"titles": [{"text": "标题", "style": "直给"}], "body": "正文",
            "tags": ["#羽毛球"], "cover_text": "封面", "first_3s": "开头",
            "shot_list": ["先拍球场"], "compliance_notes": ["别用明星肖像"]}


def make_result(*, latency_ms: float = 20_000.0, cost_cny: float = 0.03,
                coverage_ratio: float | None = 1.0, must_matched: int = 1,
                must_total: int = 1, expected_matched: int = 2,
                expected_total: int = 2, grounded_precision: float | None = 1.0) -> dict:
    return {"case": "case-01", "error": "", "top5_hit": True, "first_hit": True,
            "grounded_precision": grounded_precision, "must_matched": must_matched,
            "must_total": must_total, "expected_matched": expected_matched,
            "expected_total": expected_total, "produced_elements": 2, "candidates": 1,
            "hallucinations": 0, "structure_ok": True, "structure_problems": [],
            "tool_ok": True, "tool_problems": [], "latency_ms": latency_ms,
            "cost_cny": cost_cny, "coverage_ratio": coverage_ratio}


def make_report(ev, *, same: bool = False, diff: str = "", note: str = "",
                baseline: dict | None = None, candidate: dict | None = None) -> dict:
    baseline_metrics = ev.aggregate_dimensions([baseline or make_result()])
    candidate_metrics = ev.aggregate_dimensions([candidate or make_result()])
    gates = ev.evaluate_gates(baseline_metrics, candidate_metrics)
    return {
        "task": "hotspot_clue",
        "generated_at": "2026-10-03T12:00:00+08:00",
        "eval_version": "v1",
        "manifest_frozen_at": "2026-10-02",
        "cases_run": 1,
        "cases_total": 20,
        "limit": None,
        "note": note,
        "verdict": ev.verdict(gates),
        "gates": gates,
        "prompt": {"baseline_ref": "HEAD", "baseline_version": 1, "candidate_version": 1,
                   "same_as_baseline": same, "diff": diff},
        "arms": {"baseline_metrics": baseline_metrics, "candidate_metrics": candidate_metrics,
                 "baseline_versions": [1], "candidate_versions": [1]},
        "cases": {"case-01": {"baseline": baseline or make_result(),
                              "candidate": candidate or make_result()}},
        "environment": {"postgres_image": "pgvector/pgvector:0.8.6-pg16", "python": "3.12",
                        "llm": "openai_compatible / deepseek-flash",
                        "embedding_model": "text-embedding-v3",
                        "embedding_price_in_per_m": 0.5},
    }


class TestPromptVersionAndNaming:
    def test_reads_version_header(self, ev):
        assert ev.read_prompt_version("<!-- prompt-version: v3 -->\n正文") == 3

    def test_missing_header_raises(self, ev):
        with pytest.raises(ev.EvalError, match="prompt-version"):
            ev.read_prompt_version("没有版本头")

    def test_report_path_follows_contract(self, ev, tmp_path: Path):
        path = ev.report_path("copy_draft", 4, "2026-10-03", tmp_path)
        assert path.name == "prompt-copy_draft-v4-2026-10-03.md"

    def test_candidate_text_comes_from_the_package(self, ev):
        assert ev.read_prompt_version(ev.candidate_prompt_text("hotspot_clue")) >= 1

    def test_unknown_task_is_rejected(self, ev):
        with pytest.raises(ev.EvalError):
            ev.candidate_prompt_text("no_such_task")


class TestScoreCaseDimensions:
    def test_everything_grounded_passes_all_checks(self, ev):
        scored = ev.score_case_dimensions(make_case(), clue=make_clue(("topic", "羽毛球")),
                                          candidates=[make_candidate()], draft=make_draft())
        assert scored["must_matched"] == 1 and scored["must_total"] == 1
        assert scored["grounded_precision"] == 1.0
        assert scored["hallucinations"] == 0
        assert scored["structure_ok"] is True and scored["tool_ok"] is True

    def test_forbidden_element_counts_as_hallucination(self, ev):
        case = make_case(forbidden=(("ip", "具体艺人姓名"),))
        scored = ev.score_case_dimensions(case, clue=make_clue(("ip", "具体艺人姓名")),
                                          candidates=[make_candidate()], draft=make_draft())
        assert scored["hallucinations"] == 1
        assert scored["forbidden_hits"] == ["ip:具体艺人姓名"]

    def test_must_not_material_counts_as_hallucination(self, ev):
        case = make_case(must_not=("美食/家常菜.jpg",))
        scored = ev.score_case_dimensions(case, clue=make_clue(("topic", "羽毛球")),
                                          candidates=[make_candidate("美食/家常菜.jpg")],
                                          draft=make_draft())
        assert scored["hallucinations"] == 1
        assert scored["must_not_hits"] == ["美食/家常菜.jpg"]

    def test_must_miss_is_recorded(self, ev):
        scored = ev.score_case_dimensions(make_case(), clue=make_clue(("scene", "球场")),
                                          candidates=[make_candidate()], draft=make_draft())
        assert scored["must_matched"] == 0 and scored["must_total"] == 1

    def test_fuzzy_value_match_counts_as_a_hit(self, ev):
        """标注写「健身」、模型产出「健身房撸铁」算命中（归一化后互相包含）。"""
        case = make_case(must=(("topic", "健身"),), allowed=())
        scored = ev.score_case_dimensions(case, clue=make_clue(("topic", "健身房撸铁")),
                                          candidates=[make_candidate()], draft=make_draft())
        assert scored["must_matched"] == 1

    def test_extra_ungrounded_elements_only_lower_precision(self, ev):
        """多产出的要素只影响观察项「可回溯率」，不影响契约口径的事实准确率。"""
        clue = make_clue(("topic", "羽毛球"), ("emotion", "编出来的情绪"))
        scored = ev.score_case_dimensions(make_case(), clue=clue,
                                          candidates=[make_candidate()], draft=make_draft())
        assert scored["must_matched"] == 1          # 事实准确率仍算命中
        assert scored["grounded_precision"] == 0.5  # 观察项下降

    def test_missing_reasons_breaks_structure(self, ev):
        scored = ev.score_case_dimensions(make_case(), clue=make_clue(("topic", "羽毛球")),
                                          candidates=[make_candidate(reasons=[])],
                                          draft=make_draft())
        assert scored["structure_ok"] is False
        assert any("reasons" in problem for problem in scored["structure_problems"])

    def test_empty_clue_breaks_structure(self, ev):
        scored = ev.score_case_dimensions(make_case(), clue={}, candidates=[], draft=None)
        assert scored["structure_ok"] is False
        assert scored["grounded_precision"] is None

    def test_candidates_from_a_single_channel_are_a_tool_violation(self, ev):
        scored = ev.score_case_dimensions(make_case(), clue=make_clue(("topic", "羽毛球")),
                                          candidates=[make_candidate(sources=("literal",))],
                                          draft=make_draft())
        assert scored["tool_ok"] is False
        assert any("recall_both_channels" in problem for problem in scored["tool_problems"])

    def test_candidates_from_both_channels_pass(self, ev):
        scored = ev.score_case_dimensions(
            make_case(), clue=make_clue(("topic", "羽毛球")),
            candidates=[make_candidate(sources=("literal",)),
                        make_candidate(path="运动/球拍特写.jpg", sources=("vector",))],
            draft=make_draft())
        assert scored["tool_ok"] is True

    def test_full_gap_case_must_not_pad_candidates(self, ev):
        case = make_case(must_hit=())
        scored = ev.score_case_dimensions(case, clue=make_clue(("topic", "羽毛球")),
                                          candidates=[make_candidate()], draft=make_draft())
        assert scored["top5_hit"] is None and scored["first_hit"] is None
        assert any("no_candidate_padding" in problem for problem in scored["tool_problems"])

    def test_full_gap_case_without_candidates_is_fine(self, ev):
        scored = ev.score_case_dimensions(make_case(must_hit=()), clue=make_clue(("topic", "羽毛球")),
                                          candidates=[], draft=None)
        assert scored["tool_ok"] is True and scored["structure_ok"] is True


class TestAggregate:
    def test_aggregates_fact_accuracy_across_cases(self, ev):
        """事实准确率 = Σ must 命中 ÷ Σ must 总数（跨用例汇总）。"""
        results = [make_result(must_matched=1, must_total=1),
                   {**make_result(must_matched=1, must_total=2), "top5_hit": False,
                    "first_hit": False}]
        metrics = ev.aggregate_dimensions(results)
        assert metrics["fact_accuracy"] == round(2 / 3, 4)

    def test_aggregates_rates_and_means(self, ev):
        results = [make_result(),
                   {**make_result(latency_ms=40_000.0, cost_cny=0.01), "top5_hit": False,
                    "first_hit": False, "hallucinations": 2}]
        metrics = ev.aggregate_dimensions(results)
        assert metrics["cases"] == 2 and metrics["scored_cases"] == 2
        assert metrics["top5_hit_rate"] == 0.5 and metrics["first_hit_rate"] == 0.5
        assert metrics["hallucinations"] == 2
        assert metrics["mean_latency_ms"] == 30_000.0
        assert metrics["max_latency_ms"] == 40_000.0
        assert metrics["fact_accuracy"] == 1.0
        assert metrics["grounded_precision"] == 1.0

    def test_full_gap_cases_are_excluded_from_hit_rates(self, ev):
        results = [make_result(), {**make_result(), "top5_hit": None, "first_hit": None}]
        metrics = ev.aggregate_dimensions(results)
        assert metrics["scored_cases"] == 1
        assert metrics["top5_hit_rate"] == 1.0


class TestGates:
    def _gates(self, ev, *, baseline=None, candidate=None) -> list[dict]:
        baseline_metrics = ev.aggregate_dimensions([baseline or make_result()])
        candidate_metrics = ev.aggregate_dimensions([candidate or make_result()])
        return ev.evaluate_gates(baseline_metrics, candidate_metrics)

    def test_clean_run_is_admitted(self, ev):
        gates = self._gates(ev)
        assert ev.verdict(gates) == "准入"
        assert all(gate["passed"] for gate in gates if gate["gated"])

    def test_hallucination_forces_rollback(self, ev):
        gates = self._gates(ev, candidate={**make_result(), "hallucinations": 1})
        assert ev.verdict(gates) == "回退"

    def test_accuracy_drop_forces_rollback(self, ev):
        gates = self._gates(ev, baseline=make_result(),
                           candidate={**make_result(must_matched=0, must_total=1)})
        assert ev.verdict(gates) == "回退"

    def test_recall_drop_forces_rollback(self, ev):
        gates = self._gates(ev, candidate={**make_result(), "top5_hit": False,
                                           "first_hit": False})
        assert ev.verdict(gates) == "回退"

    def test_structure_failure_forces_rollback(self, ev):
        gates = self._gates(ev, candidate={**make_result(), "structure_ok": False})
        assert ev.verdict(gates) == "回退"

    def test_tool_failure_forces_rollback(self, ev):
        gates = self._gates(ev, candidate={**make_result(), "tool_ok": False})
        assert ev.verdict(gates) == "回退"

    def test_cost_and_latency_limits(self, ev):
        assert ev.verdict(self._gates(ev, candidate={**make_result(), "cost_cny": 0.06})) == "回退"
        assert ev.verdict(
            self._gates(ev, candidate={**make_result(), "latency_ms": 61_000.0})) == "回退"

    def test_copy_quality_is_recorded_but_not_gated(self, ev):
        gates = self._gates(ev)
        copy_gate = next(gate for gate in gates if gate["dimension"] == "文案可用率")
        assert copy_gate["gated"] is False
        assert "待人工" in str(copy_gate["candidate"])
        assert ev.verdict(gates) == "准入"      # 缺这一项不影响判定

    def test_unobservable_tool_expectations_are_listed_as_na(self, ev):
        """N/A 的 Tool 期望不进门槛，但要在报告里逐条写明原因。"""
        text = ev.render_markdown(make_report(ev))
        for name, reason in ev.TOOL_EXPECTATIONS_NA.items():
            assert name in text and reason in text
        assert "N/A" in text


class TestRenderMarkdown:
    def test_renders_all_sections(self, ev):
        text = ev.render_markdown(make_report(ev, note="收紧口吻约束"))
        for marker in ("Prompt 变更回归报告", "门槛判定", "主表", "逐用例变化",
                       "prompt 版本与改动", "已知偏差", "文案可用率", "N/A",
                       "收紧口吻约束", "结论：**准入**"):
            assert marker in text

    def test_warns_when_both_arms_are_identical(self, ev):
        text = ev.render_markdown(make_report(ev, same=True))
        assert "两臂 prompt 正文相同" in text
        assert "没有 diff" in text

    def test_includes_diff_and_versions(self, ev):
        text = ev.render_markdown(make_report(ev, diff="- 旧\n+ 新"))
        assert "- 旧" in text and "+ 新" in text
        assert "基线臂使用版本：v1" in text and "候选臂使用版本：v1" in text

    def test_notes_failed_cases(self, ev):
        candidate = {**make_result(), "error": "LLMError: 上游 503"}
        text = ev.render_markdown(make_report(ev, candidate=candidate))
        assert "运行失败" in text


class TestWriteReport:
    def test_writes_md_and_json_with_contract_name(self, ev, tmp_path: Path):
        md_path, json_path = ev.write_report(make_report(ev, diff="x"), out_dir=tmp_path)
        assert md_path.name == "prompt-hotspot_clue-v1-2026-10-03.md"
        assert json_path.name == "prompt-hotspot_clue-v1-2026-10-03.json"
        assert md_path.is_file() and json_path.is_file()
