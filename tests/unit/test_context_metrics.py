"""A09 固定事件复算（§4.1）的纯函数回归：不连库、不联网。

固定事件集：同一会话 / 模型 / prompt / policy / 实验臂，创建 generation G；受理 r1/r2/r3
三个请求，均 hit G 且各消费一次。再记录支持前缀分项的尝试 a/b 各 started，只有 a finished
（input=100, hit=60, miss=40），b 无终态。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "context_metrics.py"
SESSION = "s-1"
BASE = datetime(2026, 10, 17, 0, 0, tzinfo=UTC)
SINCE = BASE - timedelta(hours=1)
UNTIL = BASE + timedelta(hours=1)


@pytest.fixture(scope="module")
def metrics():
    spec = importlib.util.spec_from_file_location("context_metrics_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass + `from __future__ import annotations` 需要模块在 sys.modules 里才能解析注解
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(metrics, *, event_id, event_type, offset, payload=None, turn_id="t", call_id="c1",
         attempt=None):
    return metrics.Observation(
        event_id=event_id,
        session_id=SESSION,
        event_type=event_type,
        created_at=BASE + timedelta(seconds=offset),
        turn_id=turn_id,
        call_id=call_id,
        attempt=attempt,
        payload=payload or {},
    )


def fixed_events(metrics):
    rows = [
        _row(metrics, event_id="created-1", event_type="cache_entry_created",
             offset=0, payload={"generation_id": "g1"}),
        _row(metrics, event_id="req-1", event_type="cache_request_started", offset=1,
             call_id="c1"),
        _row(metrics, event_id="req-2", event_type="cache_request_started", offset=2,
             call_id="c2"),
        _row(metrics, event_id="req-3", event_type="cache_request_started", offset=3,
             call_id="c3"),
        _row(metrics, event_id="look-1", event_type="cache_lookup_finished", offset=4,
             call_id="c1", payload={"outcome": "hit", "level": "persistent"}),
        _row(metrics, event_id="look-2", event_type="cache_lookup_finished", offset=5,
             call_id="c2", payload={"outcome": "hit", "level": "persistent"}),
        _row(metrics, event_id="look-3", event_type="cache_lookup_finished", offset=6,
             call_id="c3", payload={"outcome": "hit", "level": "persistent"}),
        _row(metrics, event_id="use-1", event_type="artifact_consumed", offset=7,
             call_id="c1", payload={"generation_id": "g1", "used_for": "image_clue"}),
        _row(metrics, event_id="use-2", event_type="artifact_consumed", offset=8,
             call_id="c2", payload={"generation_id": "g1", "used_for": "image_clue"}),
        _row(metrics, event_id="use-3", event_type="artifact_consumed", offset=9,
             call_id="c3", payload={"generation_id": "g1", "used_for": "image_clue"}),
        _row(metrics, event_id="a-start", event_type="model_attempt_started", offset=10,
             payload={"identity": "call-a"}),
        _row(metrics, event_id="b-start", event_type="model_attempt_started", offset=11,
             payload={"identity": "call-b"}),
        _row(metrics, event_id="a-end", event_type="model_attempt_finished", offset=12,
             payload={"identity": "call-a", "status": "succeeded",
                      "cache_status": "complete", "cache_hit_tokens": 60,
                      "cache_miss_tokens": 40}),
    ]
    return rows


class TestFixedEventRecompute:
    def test_request_level_rates(self, metrics) -> None:
        app, prefix = metrics.analyze(fixed_events(metrics), since=SINCE, until=UNTIL)
        report = app.as_dict()
        assert report["C"] == 3
        assert (report["H"], report["M"], report["E"], report["B"]) == (3, 0, 0, 0)
        # 三次命中各消费一次 → 3/3 = 100%（旧口径按 generation 只有 1/3）
        assert report["hit_consumption_rate"] == 1.0
        assert report["hit_consumption_numerator"] == 3
        assert report["entry_reuse_rate"] == 1.0
        assert report["normal_observation_completeness"] == 1.0

    def test_prefix_attempt_coverage(self, metrics) -> None:
        app, prefix = metrics.analyze(fixed_events(metrics), since=SINCE, until=UNTIL)
        _ = app
        report = prefix.as_dict()
        assert report["K"] == 1
        assert report["attempts_started"] == 2
        assert report["attempts_finished"] == 1
        assert report["attempts_without_terminal"] == 1
        assert report["supported_complete_coverage"] == 0.5     # 1/2
        assert report["all_attempt_coverage"] == 0.5
        assert report["supported_success_coverage"] == 1.0      # 1/1，不混为全部尝试
        assert report["unknown_ratio"] == 0.5

    def test_strict_fails_on_missing_terminal(self, metrics) -> None:
        app, prefix = metrics.analyze(fixed_events(metrics), since=SINCE, until=UNTIL)
        report = {
            "app_cache": app.as_dict(), "prefix_cache": prefix.as_dict(),
            "filters": {}, "totals": {"observations": 13},
        }
        failures = metrics._strict_gates(report)
        assert any("缺少终态" in item for item in failures)

    def test_strict_rejects_empty_report(self, metrics) -> None:
        app, prefix = metrics.analyze([], since=SINCE, until=UNTIL)
        report = {
            "app_cache": app.as_dict(), "prefix_cache": prefix.as_dict(),
            "filters": {}, "totals": {"observations": 0},
        }
        failures = metrics._strict_gates(report)
        assert any("空报告" in item for item in failures)

    def test_duplicate_delivery_counted_once(self, metrics) -> None:
        rows = fixed_events(metrics)
        rows.append(rows[0])          # 同一 event_id 重复投递
        app, _ = metrics.analyze(rows, since=SINCE, until=UNTIL)
        report = app.as_dict()
        assert report["C"] == 3
        assert report["duplicate_events_ignored"] == 1

    def test_consumption_after_queue_end_still_counts(self, metrics) -> None:
        rows = fixed_events(metrics)
        app, _ = metrics.analyze(rows, since=SINCE, until=UNTIL)
        report = app.as_dict()
        assert report["entry_queue"]["reused_in_queue"] == 1

    def test_request_outside_window_excluded(self, metrics) -> None:
        app, _ = metrics.analyze(
            fixed_events(metrics), since=BASE + timedelta(seconds=100), until=UNTIL
        )
        assert app.as_dict()["C"] == 0

    def test_selected_edit_is_not_cache_consumption(self, metrics) -> None:
        rows = fixed_events(metrics)
        for row in rows:
            if row.event_type == "artifact_consumed":
                object.__setattr__(row, "payload", {"generation_id": "g1",
                                                    "used_for": "selected_clue"})
        app, _ = metrics.analyze(rows, since=SINCE, until=UNTIL)
        report = app.as_dict()
        assert report["hit_consumption_rate"] == 0.0
        # 创建队列存在但没有任何真实消费 → 0/1（不是 N/A）
        assert report["entry_reuse_rate"] == 0.0
