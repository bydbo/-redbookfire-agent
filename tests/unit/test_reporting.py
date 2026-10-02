"""报告用例单测：model 形状、热点条目、落盘产物（S3.1）。"""

from __future__ import annotations

from pathlib import Path

from xhs_agent.services import reporting
from xhs_agent.tools.trace import RunStore


def hotspot() -> dict:
    return {
        "clue": {"hotspot_raw": "某明星打羽毛球", "elements": [
            {"type": "topic", "value": "羽毛球", "weight": 0.9, "confidence": 0.9}]},
        "coverage": {"ratio": 1.0, "covered": [], "gaps": []},
        "candidates": [{"rank": 1, "score": 0.8, "reasons": ["命中主题"], "usage": "放开头",
                        "material": {"id": "m_1", "path": "D:/materials/a.mp4",
                                     "title": "球场热身"}}],
        "draft": None,
        "error": "",
    }


class TestBuildReportModel:
    def test_shape_matches_report_contract(self):
        model = reporting.build_report_model(
            run_id="run-1", created_at="2026-10-02T10:00:00+08:00", materials_count=19,
            config={"llm": {"provider": "openai_compatible", "model": "deepseek-flash"},
                    "materials_dir": "D:/materials"},
            hotspots=[hotspot()], totals={"llm_calls": 3}, errors=[])
        assert set(model) == {"meta", "config", "hotspots", "totals", "errors"}
        assert model["meta"] == {"run_id": "run-1", "created_at": "2026-10-02T10:00:00+08:00",
                                 "materials_count": 19}
        assert model["hotspots"][0]["clue"]["hotspot_raw"] == "某明星打羽毛球"

    def test_vector_coverage_is_optional(self):
        base = {"run_id": "r", "created_at": "t", "materials_count": 1, "config": {},
                "hotspots": [], "totals": {}, "errors": []}
        assert "vector_coverage" not in reporting.build_report_model(**base)["meta"]
        with_coverage = reporting.build_report_model(
            **base, vector_coverage={"enabled": True, "total": 3, "with_embedding": 3,
                                     "ratio": 1.0})
        assert with_coverage["meta"]["vector_coverage"]["ratio"] == 1.0

    def test_returns_copies_not_aliases(self):
        hotspots = [hotspot()]
        errors = ["e"]
        model = reporting.build_report_model(run_id="r", created_at="t", materials_count=0,
                                             config={}, hotspots=hotspots, totals={},
                                             errors=errors)
        model["hotspots"].append({})
        model["errors"].append("x")
        assert len(hotspots) == 1 and errors == ["e"]


class TestHotspotEntry:
    def test_minimal_entry_shape(self):
        entry = reporting.hotspot_entry(clue={}, coverage={}, candidates=[])
        assert entry["draft"] is None
        assert entry["error"] == ""
        assert "gap_advice" not in entry  # 空建议不写键，报告回退模板句

    def test_carries_gap_advice_and_error(self):
        entry = reporting.hotspot_entry(clue={"hotspot_raw": "x"}, coverage={"ratio": 0.0},
                                        candidates=[], gap_advice=[{"type": "ip", "value": "明星",
                                                                    "label": "人物IP",
                                                                    "advice": "别用肖像"}],
                                        error="boom")
        assert entry["gap_advice"][0]["advice"] == "别用肖像"
        assert entry["error"] == "boom"


class TestRenderAndSave:
    def test_writes_markdown_and_html(self, tmp_path: Path):
        store = RunStore(str(tmp_path / "runs"), "球场热身")
        model = reporting.build_report_model(
            run_id=store.run_id, created_at="2026-10-02T10:00:00+08:00", materials_count=1,
            config={"llm": {"provider": "p", "model": "m"}, "materials_dir": "D:/m"},
            hotspots=[hotspot()], totals={"llm_calls": 1}, errors=[])
        markdown_path, html_path = reporting.render_and_save(model, store, base_dir=str(tmp_path))
        assert Path(markdown_path).is_file() and Path(html_path).is_file()
        text = Path(markdown_path).read_text(encoding="utf-8")
        assert "某明星打羽毛球" in text
        assert "球场热身" in text
        assert set(store.state["artifacts"]) >= {"report.md", "report.html"}
