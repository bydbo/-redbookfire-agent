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

    def test_running_after_terminal_restarts_the_stage(self):
        snapshot = progress.apply(None, stage="clue", status="failed", error_category="llm_error")
        assert snapshot["error_category"] == "llm_error"
        retried = progress.apply(snapshot, stage="clue", status="running")
        assert retried["status"] == "running" and retried["error_category"] == ""

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
