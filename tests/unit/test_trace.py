"""运行追踪单测：结构、计数、落盘。时间戳只断言「有值」，不断言具体时间。"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from xhs_agent.tools import trace


@pytest.fixture
def store(tmp_path):
    return trace.RunStore(str(tmp_path / "runs"), "冒烟跑")


class TestInit:
    def test_run_id_and_directory(self, store, tmp_path):
        assert re.fullmatch(r"\d{8}-\d{6}-.+", store.run_id)
        assert pathlib.Path(store.dir).parent == tmp_path / "runs"
        assert pathlib.Path(store.dir).is_dir()

    def test_initial_state_shape(self, store):
        assert {"run_id", "created_at", "status", "meta", "steps", "artifacts",
                "totals", "notes"} <= set(store.state)
        assert store.state["status"] == "running"
        assert store.state["totals"] == {"llm_calls": 0, "prompt_tokens": 0,
                                         "completion_tokens": 0, "cost_cny": 0.0,
                                         "latency_ms": 0}

    def test_meta_is_kept(self, tmp_path):
        store = trace.RunStore(str(tmp_path), "x", meta={"provider": "openai_compatible"})
        assert store.state["meta"] == {"provider": "openai_compatible"}

    def test_trace_file_exists(self, store):
        assert pathlib.Path(store.trace_path).is_file()


class TestSteps:
    def test_successful_step_is_closed(self, store):
        with store.step("拆解", "线索"):
            pass
        step = store.state["steps"][0]
        assert (step["name"], step["detail"], step["status"]) == ("拆解", "线索", "succeeded")
        assert step["ended_at"]
        assert step["latency_ms"] >= 0

    def test_failed_step_records_error_and_reraises(self, store):
        with pytest.raises(ValueError), store.step("检索"):
            raise ValueError("炸了")
        step = store.state["steps"][0]
        assert step["status"] == "failed"
        assert step["error"] == "ValueError: 炸了"

    def test_base_exception_is_recorded_as_failed(self, store):
        """KeyboardInterrupt / SystemExit 这类 BaseException 不能被记成成功。

        原先只捕 Exception，中断时状态停在 running，再被 finally 回填成 succeeded，
        运行记录会失真（S1.4 复核发现）。
        """
        with pytest.raises(KeyboardInterrupt), store.step("检索"):
            raise KeyboardInterrupt
        step = store.state["steps"][0]
        assert step["status"] == "failed"
        assert step["error"] == "KeyboardInterrupt: "

    def test_base_exception_status_is_persisted(self, store):
        with pytest.raises(SystemExit), store.step("报告"):
            raise SystemExit(2)
        payload = json.loads(pathlib.Path(store.dir, "state.json").read_text(encoding="utf-8"))
        assert payload["steps"][0]["status"] == "failed"

    def test_steps_are_kept_in_order(self, store):
        with store.step("一"):
            pass
        with store.step("二"):
            pass
        assert [step["name"] for step in store.state["steps"]] == ["一", "二"]


class TestLLMRecords:
    def test_totals_accumulate(self, store):
        store.record_llm({"task": "t", "prompt_tokens": 10, "completion_tokens": 4,
                          "cost_cny": 0.001})
        store.record_llm({"task": "t", "prompt_tokens": 5, "completion_tokens": 1,
                          "cost_cny": 0.002})
        assert store.state["totals"]["llm_calls"] == 2
        assert store.state["totals"]["prompt_tokens"] == 15
        assert store.state["totals"]["completion_tokens"] == 5
        assert store.state["totals"]["cost_cny"] == pytest.approx(0.003)

    def test_record_is_attributed_to_current_step(self, store):
        with store.step("撰稿"):
            store.record_llm({"prompt_tokens": 7, "completion_tokens": 3, "cost_cny": 0.001})
        step = store.state["steps"][0]
        assert (step["llm_calls"], step["prompt_tokens"], step["completion_tokens"]) == (1, 7, 3)


class TestArtifacts:
    def test_save_json_and_text_register_artifacts(self, store):
        json_path = store.save_json("data.json", {"a": 1})
        text_path = store.save_text("report.md", "# 标题")
        assert json.loads(pathlib.Path(json_path).read_text(encoding="utf-8")) == {"a": 1}
        assert pathlib.Path(text_path).read_text(encoding="utf-8") == "# 标题"
        assert store.state["artifacts"] == ["data.json", "report.md"]

    def test_artifact_is_also_attached_to_step(self, store):
        with store.step("报告"):
            store.save_text("report.md", "x")
        assert store.state["steps"][0]["artifacts"] == ["report.md"]


class TestPlanAndFinalize:
    def test_pending_lists_planned_but_unfinished(self, store):
        store.plan(["拆解", "检索", "撰稿"])
        with store.step("拆解"):
            pass
        store.save_state()
        assert store.state["pending"] == ["检索", "撰稿"]

    def test_finalize_writes_state_file(self, store):
        path = store.finalize(status="succeeded", notes="完成")
        assert path.endswith("state.json")
        payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        assert payload["status"] == "succeeded"
        assert payload["finished_at"]
        assert payload["notes"] == "完成"

    def test_trace_lines_are_json_events(self, store):
        store.finalize()
        lines = pathlib.Path(store.trace_path).read_text(encoding="utf-8").strip().splitlines()
        events = [json.loads(line) for line in lines]
        assert events[0]["event"] == "run_started"
        assert events[-1]["event"] == "run_finished"
        assert all({"ts", "event", "payload"} <= set(event) for event in events)

    def test_summary_lines_cover_totals(self, store):
        store.record_llm({"prompt_tokens": 10, "completion_tokens": 5, "cost_cny": 0.5})
        lines = store.summary_lines()
        assert len(lines) == 5
        assert any(store.run_id in line for line in lines)
        assert any("0.5000" in line for line in lines)
