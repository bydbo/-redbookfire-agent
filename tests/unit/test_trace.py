"""运行追踪单测：结构、计数、事件流。时间戳只断言「有值」，不断言具体时间。

ADR 0011 起 `RunStore` **不再落 `state.json`**（运行状态以数据库为准），本文件据此更新：
只断言 `trace.jsonl` 的事件与内存态，并新增"产物目录里没有 state.json"与"注入 run_id"两条。
"""

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

    def test_injected_run_id_becomes_directory_name(self, tmp_path):
        """worker 传 DB 的 run_id：产物目录就是 runs/<run_id>/（ADR 0011）。"""
        run_id = "362cc22a-8d5e-4b7d-8a92-857cb6705f2c"
        store = trace.RunStore(str(tmp_path / "runs"), "忽略的 slug", run_id=run_id)
        assert store.run_id == run_id
        assert pathlib.Path(store.dir) == tmp_path / "runs" / run_id
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

    def test_base_exception_is_logged_to_trace(self, store):
        """中断也要留事件流（state.json 已停写，事件流是唯一的落盘证据）。"""
        with pytest.raises(SystemExit), store.step("报告"):
            raise SystemExit(2)
        events = [json.loads(line) for line
                  in pathlib.Path(store.trace_path).read_text(encoding="utf-8").splitlines()]
        failed = [event for event in events if event["event"] == "step_failed"]
        assert failed and failed[0]["payload"]["name"] == "报告"
        assert "SystemExit" in failed[0]["payload"]["error"]

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
        assert store.pending_steps() == ["检索", "撰稿"]

    def test_finalize_marks_status_and_returns_run_dir(self, store):
        path = store.finalize(status="succeeded", notes="完成")
        assert path == store.dir
        assert store.state["status"] == "succeeded"
        assert store.state["finished_at"]
        assert store.state["notes"] == "完成"

    def test_no_state_json_is_written(self, store):
        """ADR 0011：运行状态以数据库为准，产物目录里不许再出现 state.json。"""
        with store.step("拆解"):
            pass
        store.save_text("report.md", "x")
        store.finalize()
        assert not pathlib.Path(store.dir, "state.json").exists()
        assert sorted(item.name for item in pathlib.Path(store.dir).iterdir()) == \
            ["report.md", "trace.jsonl"]

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
