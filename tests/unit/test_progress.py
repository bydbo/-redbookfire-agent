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


class TestIdentityIsolation:
    """R5（E7OBS2-02）：迟到终态不回退、不串身份；冲突终态保留首个；身份随当前阶段一致。"""

    def test_late_terminal_from_another_run_only_fills_nodes(self):
        """report/running/run-new 收到 draft/succeeded/run-old：顶层不回退，历史照补。"""
        snapshot = progress.begin(None, stage="report", attempt=1, run_id="run-new",
                                  job_id="job-new")
        late = progress.end(snapshot, stage="draft", status="succeeded", attempt=2,
                            run_id="run-old")
        assert late["stage"] == "report" and late["status"] == "running"
        assert late["run_id"] == "run-new" and late["job_id"] == "job-new"
        nodes = {(item["stage"], item.get("run_id") or ""): item["status"]
                 for item in late["nodes"]}
        assert nodes[("draft", "run-old")] == "succeeded"      # 历史事实照补，不回退顶层

    def test_same_stage_terminal_from_another_run_does_not_take_over(self):
        """同 stage 新 run 收旧 run 结束：当前状态与身份都不被改写。"""
        snapshot = progress.begin(None, stage="clue", attempt=1, run_id="run-new")
        late = progress.end(snapshot, stage="clue", status="failed", attempt=5,
                            run_id="run-old", error_category="llm_error")
        assert late["status"] == "running" and late["attempt"] == 1
        assert late["run_id"] == "run-new" and late["error_category"] == ""
        assert late["nodes"][0]["run_id"] == "run-old"         # 旧执行自己的节点

    def test_new_run_with_lower_attempt_starts_its_own_execution(self):
        """新 run 尝试号更低：不被旧 run 的高尝试 / 终态拦截（身份优先于序号）。"""
        old = progress.begin(None, stage="clue", attempt=3, run_id="run-old")
        old = progress.end(old, stage="clue", status="failed", attempt=3, run_id="run-old",
                           error_category="llm_error")
        fresh = progress.begin(old, stage="clue", attempt=1, run_id="run-new")
        assert fresh["status"] == "running" and fresh["attempt"] == 1
        assert fresh["run_id"] == "run-new" and fresh["error_category"] == ""

    def test_duplicate_terminal_same_status_refreshes_latency(self):
        """同状态重复终态：允许耗时修订（终态耗时来源后补的场景）。"""
        snapshot = progress.end(progress.begin(None, stage="report", attempt=1,
                                               run_id="run-a"),
                                stage="report", status="succeeded", attempt=1,
                                run_id="run-a", latency_ms=5)
        refreshed = progress.end(snapshot, stage="report", status="succeeded", attempt=1,
                                 run_id="run-a", latency_ms=99)
        assert refreshed["nodes"][0]["latency_ms"] == 99
        assert refreshed["status"] == "succeeded"

    def test_conflicting_terminal_keeps_first_status(self):
        """冲突终态（同执行同尝试）：保留首个，不翻转、不混状态。"""
        snapshot = progress.end(progress.begin(None, stage="clue", attempt=1, run_id="run-a"),
                                stage="clue", status="failed", attempt=1, run_id="run-a",
                                error_category="llm_error")
        second = progress.end(snapshot, stage="clue", status="succeeded", attempt=1,
                              run_id="run-a")
        assert second["status"] == "failed"
        assert second["error_category"] == "llm_error"
        assert second["nodes"][0]["status"] == "failed"

    def test_identity_follows_current_stage(self):
        """顶层 run/job 身份与当前阶段一致：同阶段缺身份保留，跨阶段缺身份清空。"""
        started = progress.begin(None, stage="clue", attempt=1, run_id="run-a", job_id="job-a")
        closed = progress.end(started, stage="clue", status="succeeded")
        assert closed["run_id"] == "run-a" and closed["job_id"] == "job-a"
        moved = progress.end(closed, stage="report", status="succeeded", run_id="run-a")
        assert moved["stage"] == "report" and moved["run_id"] == "run-a"
        left = progress.end(moved, stage="final_reply", status="succeeded")
        assert left["run_id"] is None and left["job_id"] is None   # 跨阶段缺身份：清空

    def test_cross_stage_advance_resets_error_category(self):
        """跨阶段推进不残留上一阶段的错误类别。"""
        failed = progress.end(progress.begin(None, stage="clue", attempt=1, run_id="run-a"),
                              stage="clue", status="failed", attempt=1, run_id="run-a",
                              error_category="llm_error")
        moved = progress.end(progress.begin(failed, stage="report", attempt=1, run_id="run-a"),
                             stage="report", status="succeeded", run_id="run-a")
        assert moved["stage"] == "report" and moved["error_category"] == ""

    def test_same_source_late_stage_does_not_take_over_current(self):
        current = progress.begin(
            None, stage="report", run_id="run-a", source_id="source-a",
            stage_instance_id="source-a:report:1", source_seq=5,
        )
        late = progress.end(
            current, stage="draft", status="succeeded", run_id="run-a",
            source_id="source-a", stage_instance_id="source-a:draft:1", source_seq=3,
        )
        assert (late["stage"], late["status"], late["run_id"]) == (
            "report", "running", "run-a"
        )
        assert any(
            item["stage"] == "draft" and item["status"] == "succeeded"
            for item in late["nodes"]
        )

    def test_new_stage_with_new_source_can_start_at_lower_sequence(self):
        current = progress.begin(
            None, stage="report", run_id="run-a", source_id="source-a",
            stage_instance_id="source-a:report:1", source_seq=5,
        )
        advanced = progress.begin(
            current, stage="clue", run_id="run-b", source_id="source-b",
            stage_instance_id="source-b:clue:1", source_seq=1,
        )
        assert advanced["stage"] == "clue"
        assert advanced["source_id"] == "source-b"


class TestStageInstanceIdentity:
    """E7OBS4-03：stage 相同不代表实例相同；新实例能启动，旧实例只能补历史。"""

    def test_second_instance_of_same_stage_starts_after_first_ended(self):
        """C3：draft#1 成功 → report → draft#2（attempt 仍为 1）正常启动，旧终态不阻断。"""
        snapshot = progress.end(
            progress.begin(
                None, stage="draft", attempt=1, run_id="run-a", source_id="src",
                stage_instance_id="src:draft:1", source_seq=1,
            ),
            stage="draft", status="succeeded", attempt=1, run_id="run-a",
            source_id="src", stage_instance_id="src:draft:1", source_seq=2,
        )
        snapshot = progress.begin(
            snapshot, stage="report", attempt=1, run_id="run-a", source_id="src",
            stage_instance_id="src:report:1", source_seq=3,
        )
        second = progress.begin(
            snapshot, stage="draft", attempt=1, run_id="run-a", source_id="src",
            stage_instance_id="src:draft:2", source_seq=4,
        )
        assert (second["stage"], second["status"]) == ("draft", "running")
        assert second["stage_instance_id"] == "src:draft:2"
        assert second["attempt"] == 1          # 新实例有自己的起点，不继承旧尝试号

    def test_late_events_of_previous_instance_do_not_take_over(self):
        """C4：second 已活动时，first 的 begin/end 迟到 —— 不接管，历史事实按规则补录。"""
        snapshot = progress.begin(
            None, stage="draft", attempt=1, run_id="run-a", source_id="src",
            stage_instance_id="src:draft:1", source_seq=1,
        )
        snapshot = progress.end(
            snapshot, stage="draft", status="succeeded", attempt=1, run_id="run-a",
            source_id="src", stage_instance_id="src:draft:1", source_seq=2,
        )
        snapshot = progress.begin(
            snapshot, stage="draft", attempt=1, run_id="run-a", source_id="src",
            stage_instance_id="src:draft:2", source_seq=6,
        )
        late_begin = progress.begin(
            snapshot, stage="draft", attempt=1, run_id="run-a", source_id="src",
            stage_instance_id="src:draft:1", source_seq=3,
        )
        assert late_begin["stage_instance_id"] == "src:draft:2"
        assert late_begin["source_seq"] == 6
        late_end = progress.end(
            snapshot, stage="draft", status="failed", attempt=1, run_id="run-a",
            source_id="src", stage_instance_id="src:draft:1", source_seq=4,
        )
        assert (late_end["stage"], late_end["status"]) == ("draft", "running")
        assert late_end["stage_instance_id"] == "src:draft:2"
        # 冲突终态保留首个：已成功的 draft:1 不会被迟到的 failed 翻转
        first = next(
            node for node in late_end["nodes"]
            if node["stage_instance_id"] == "src:draft:1"
        )
        assert first["status"] == "succeeded"

    def test_older_generation_cannot_override_newer_execution(self):
        """C6（纯函数侧）：旧执行代次（worker 重试前那一代）的迟到写入被拒绝。"""
        newest = progress.begin(
            None, stage="clue", attempt=1, run_id="run-a", source_id="worker:run-a:g1",
            stage_instance_id="worker:run-a:g1:clue:1", source_seq=2, generation=1,
        )
        stale = progress.end(
            newest, stage="clue", status="succeeded", attempt=1, run_id="run-a",
            source_id="worker:run-a:g0", stage_instance_id="worker:run-a:g0:clue:1",
            source_seq=9, generation=0,
        )
        assert (stale["stage"], stale["status"]) == ("clue", "running")
        assert stale["generation"] == 1
        assert stale["source_seq"] == 2

    def test_node_truncation_keeps_current_state_and_guards(self):
        """C8：节点超上限时只丢最早的历史，不影响当前阶段与重复终态判断。"""
        snapshot = progress.begin(
            None, stage="draft", attempt=1, run_id="run-a", source_id="src",
            stage_instance_id="src:draft:1", source_seq=1,
        )
        for index in range(progress.MAX_NODES + 5):
            snapshot = progress.begin(
                snapshot, stage="draft", attempt=1, run_id=f"run-{index}",
                source_id=f"src-{index}", stage_instance_id=f"src-{index}:draft:1",
                source_seq=1,
            )
            snapshot = progress.end(
                snapshot, stage="draft", status="skipped", attempt=1,
                run_id=f"run-{index}", source_id=f"src-{index}",
                stage_instance_id=f"src-{index}:draft:1", source_seq=2,
            )
        assert len(snapshot["nodes"]) <= progress.MAX_NODES
        assert snapshot["stage"] == "draft"
        assert snapshot["source_id"] == f"src-{progress.MAX_NODES + 4}"
        # 重复终态不重置计时：已终结实例的 started_at 保持首个
        again = progress.end(
            snapshot, stage="draft", status="skipped", attempt=1,
            run_id=f"run-{progress.MAX_NODES + 4}",
            source_id=f"src-{progress.MAX_NODES + 4}",
            stage_instance_id=f"src-{progress.MAX_NODES + 4}:draft:1", source_seq=3,
        )
        assert again["started_at"] == snapshot["started_at"]


class TestWebToolProgressLabels:
    """PR3 · Task 10：网页工具的进度标签（后端权威口径）。

    handler 里报的是中文标签（`on_progress("搜索热点")`），tracing span 是
    `tool.web_search` / `tool.web_fetch`；两份映射必须同时登记，否则前端会退回英文键。
    """

    def test_stage_labels_carry_web_tool_keys(self):
        assert progress.STAGE_LABELS["web_search"] == "搜索热点"
        assert progress.STAGE_LABELS["web_fetch"] == "读取网页"

    def test_progress_label_keys_map_web_labels_back_to_stage_keys(self):
        assert progress.PROGRESS_LABEL_KEYS["搜索热点"] == "web_search"
        assert progress.PROGRESS_LABEL_KEYS["读取网页"] == "web_fetch"

    def test_handler_labels_round_trip_through_stage_keys(self):
        assert progress.key_for_label("搜索热点") == "web_search"
        assert progress.key_for_label("读取网页") == "web_fetch"
        assert progress.label_for("web_search") == "搜索热点"
        assert progress.label_for("web_fetch") == "读取网页"

    def test_progress_label_keys_are_backed_by_stage_labels(self):
        # 同步约束：PROGRESS_LABEL_KEYS 的值必须都是 STAGE_LABELS 的键，否则 key_for_label 会漏
        assert set(progress.PROGRESS_LABEL_KEYS.values()) <= set(progress.STAGE_LABELS)
