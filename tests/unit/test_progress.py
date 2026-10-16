"""阶段快照单测（S7.19）：合并语义、迟到更新、跳过与错误类别白名单。

离线：不连库、不联网；只验证 `core/progress.py` 的纯函数行为。
"""

from __future__ import annotations

import asyncio
import json

from xhs_agent.core import progress


class TestSnapshotLifecycle:
    def test_begin_and_end_track_state_and_nodes(self):
        snapshot = progress.begin(None, stage="retrieve")
        assert snapshot["status"] == "running" and snapshot["label"] == "检索"
        assert snapshot["started_at"] and snapshot["updated_at"]
        finished = progress.end(snapshot, stage="retrieve", status="succeeded", latency_ms=120)
        assert finished["status"] == "succeeded"
        assert finished["last_finished_stage"] == "retrieve"
        (node,) = finished["nodes"]
        assert node["stage"] == "retrieve" and node["latency_ms"] == 120
        assert node["error_category"] == "" and node["ended_at"]

    def test_repeat_running_keeps_started_at(self):
        first = progress.begin(None, stage="supervisor")
        second = progress.begin(first, stage="supervisor")
        assert second["started_at"] == first["started_at"]
        assert second["status"] == "running"

    def test_new_stage_resets_started_at(self):
        first = progress.begin(None, stage="supervisor")
        second = progress.begin(first, stage="retrieve")
        assert second["stage"] == "retrieve" and second["status"] == "running"

    def test_late_attempt_does_not_override_newer_state(self):
        snapshot = progress.begin(None, stage="retrieve", attempt=2)
        late = progress.end(snapshot, stage="retrieve", status="succeeded", attempt=1)
        assert late["status"] == "running"
        assert late["nodes"] == []

    def test_failed_stage_keeps_error_category_only(self):
        snapshot = progress.apply(None, stage="clue", status="failed",
                                  error_category="很长的错误正文")
        assert snapshot["status"] == "failed"
        assert snapshot["error_category"] == "internal"     # 白名单外一律 internal，不存正文

    def test_skipped_is_terminal_and_finishes_stage(self):
        snapshot = progress.apply(None, stage="clue", status="skipped")
        assert snapshot["status"] == "skipped"
        assert snapshot["last_finished_stage"] == "clue"

    def test_running_after_terminal_needs_a_higher_attempt(self):
        """R3（OBS-12）：同一执行终结后不因重复 begin 重开；更高的 attempt 才启动新执行。"""
        snapshot = progress.apply(None, stage="clue", status="failed", error_category="llm_error")
        assert snapshot["error_category"] == "llm_error"
        same = progress.apply(snapshot, stage="clue", status="running")
        assert same["status"] == "failed" and same["error_category"] == "llm_error"
        retried = progress.apply(snapshot, stage="clue", status="running", attempt=2)
        assert retried["status"] == "running" and retried["error_category"] == ""
        assert retried["attempt"] == 2

    def test_nodes_dedupe_and_cap(self):
        snapshot = progress.empty()
        for attempt in range(1, progress.MAX_NODES + 11):
            snapshot = progress.apply(snapshot, stage="clue", status="running", attempt=attempt)
            snapshot = progress.apply(snapshot, stage="clue", status="succeeded", attempt=attempt)
        assert len(snapshot["nodes"]) == progress.MAX_NODES
        keys = [(item["stage"], item["attempt"]) for item in snapshot["nodes"]]
        assert len(keys) == len(set(keys))

    def test_normalize_accepts_history_and_garbage(self):
        for raw in (None, {}, {"stage": "x"}, "not-a-dict", {"nodes": "bad"}):
            snapshot = progress.normalize(raw)
            assert snapshot["stage"] in ("", "x")
            assert isinstance(snapshot["nodes"], list)

    def test_interrupt_only_touches_running(self):
        running = progress.begin(None, stage="draft")
        assert progress.interrupt(running)["status"] == "interrupted"
        done = progress.end(running, stage="draft", status="succeeded")
        assert progress.interrupt(done)["status"] == "succeeded"

    def test_current_step_projection(self):
        assert progress.current_step(None) is None
        assert progress.current_step(progress.begin(None, stage="gaps")) == "缺口"
        ended = progress.end(progress.begin(None, stage="gaps"), stage="gaps",
                             status="succeeded")
        assert progress.current_step(ended) is None

    def test_unknown_stage_label_falls_back_to_key(self):
        assert progress.label_for("custom_stage") == "custom_stage"


class TestErrorCategory:
    def test_maps_known_exceptions_by_name(self):
        class LLMError(RuntimeError):
            ...

        class EmbeddingError(RuntimeError):
            ...

        assert progress.error_category(LLMError("x")) == "llm_error"
        assert progress.error_category(EmbeddingError("x")) == "embedding_error"
        assert progress.error_category(TimeoutError()) == "timeout"
        assert progress.error_category(RuntimeError("其它")) == "internal"

    def test_cancelled_error_is_a_base_exception(self):
        assert progress.error_category(asyncio.CancelledError()) == "cancelled"

    def test_whitelist_is_stable(self):
        assert "llm_error" in progress.ERROR_CATEGORIES
        assert set(progress.TERMINAL_STAGE_STATUSES) == {
            "succeeded", "failed", "skipped", "interrupted"}

    def test_error_text_paths_and_prompts_never_enter_snapshot(self):
        """S7.22 白名单：错误正文 / 路径 / URL / 提示词一律折成 `internal`，不落快照（约定 §9）。"""
        leaked = [
            "C:\\Users\\me\\素材\\私密.docx",
            "/home/me/secret/素材.png",
            "上报地址 https://cloud.langfuse.com/trace/abc",
            "提示词：把这个素材写成爆款文案",
        ]
        for text in leaked:
            snapshot = progress.apply(None, stage="retrieve", status="failed",
                                      error_category=text)
            assert snapshot["error_category"] == "internal"
            assert text not in json.dumps(snapshot, ensure_ascii=False)

    def test_every_mapped_exception_lands_in_the_whitelist(self):
        """`error_category()` 的输出恒在白名单内（含取消 / 超时这两个非 `Exception` 分支）。"""
        cases = [asyncio.CancelledError(), TimeoutError(), RuntimeError("x"),
                 OSError("x"), Exception("x"), KeyboardInterrupt()]
        for exc in cases:
            assert progress.error_category(exc) in progress.ERROR_CATEGORIES


class TestMonotonicMerge:
    """R3（OBS-12）：迟到的 begin 不回退、终态不重开、更高尝试才能启动新执行。"""

    def test_late_begin_does_not_regress_attempt(self):
        snapshot = progress.begin(None, stage="draft", attempt=2)
        late = progress.begin(snapshot, stage="draft", attempt=1)
        assert late["attempt"] == 2 and late["status"] == "running"

    def test_terminal_execution_is_not_reopened(self):
        snapshot = progress.end(progress.begin(None, stage="draft", attempt=2), stage="draft",
                                status="succeeded", attempt=2)
        again = progress.begin(snapshot, stage="draft", attempt=2)
        assert again["status"] == "succeeded"          # 已终结的执行：重复 begin 不重开

    def test_higher_attempt_starts_a_new_execution(self):
        snapshot = progress.end(progress.begin(None, stage="draft", attempt=1), stage="draft",
                                status="failed", attempt=1, error_category="llm_error")
        retry = progress.begin(snapshot, stage="draft", attempt=2)
        assert retry["status"] == "running" and retry["attempt"] == 2
        assert retry["error_category"] == ""           # 新执行不带旧失败类别

    def test_completed_execution_blocks_late_begin_across_stages(self):
        snapshot = progress.begin(None, stage="supervisor")
        snapshot = progress.end(snapshot, stage="supervisor", status="succeeded")
        snapshot = progress.begin(snapshot, stage="clue")            # 已经走到下一个阶段
        late = progress.begin(snapshot, stage="supervisor", attempt=1)
        assert late["stage"] == "clue"                 # 迟到事件不改当前阶段

    def test_run_identity_keeps_two_analyses_separate(self):
        snapshot = progress.begin(None, stage="clue", attempt=1, run_id="run-a")
        snapshot = progress.end(snapshot, stage="clue", status="succeeded", attempt=1,
                                run_id="run-a")
        second = progress.begin(snapshot, stage="clue", attempt=1, run_id="run-b")
        assert second["status"] == "running" and second["run_id"] == "run-b"
        second = progress.end(second, stage="clue", status="failed", attempt=1,
                              error_category="llm_error", run_id="run-b")
        nodes = {(item["stage"], item["attempt"], item.get("run_id") or ""): item["status"]
                 for item in second["nodes"]}
        assert nodes[("clue", 1, "run-a")] == "succeeded"   # 第一轮记录不被覆盖
        assert nodes[("clue", 1, "run-b")] == "failed"

    def test_retries_flow_into_snapshot_through_apply(self):
        snapshot = progress.apply(None, stage="material_sync", status="running", retries=2)
        assert snapshot["retries"] == 2
        snapshot = progress.apply(snapshot, stage="material_sync", status="succeeded", retries=2)
        assert snapshot["retries"] == 2

    def test_old_snapshot_without_run_id_still_merges(self):
        """兼容旧快照：没有 run_id 的节点与新事件按旧口径合并（不因缺身份拒绝更新）。"""
        legacy = {"stage": "clue", "status": "running", "attempt": 1,
                  "nodes": [{"stage": "clue", "status": "running", "attempt": 1}]}
        merged = progress.end(legacy, stage="clue", status="succeeded", attempt=1,
                              run_id="run-new")
        assert merged["status"] == "succeeded"
        assert len(merged["nodes"]) == 1               # 合并进同一个节点，不新增
