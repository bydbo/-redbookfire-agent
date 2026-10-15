"""`scripts/check_trace_chain.py` 的纯函数单测（S7.22）：离线、不连库、不联网。

覆盖核对口径本身：入口 / 工具 / worker 链、叶子用量白名单、耗时容差，以及 span 树的打印。
云端响应形状用最小字典伪造（只放脚本真正读的字段）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_trace_chain.py"


@pytest.fixture(scope="module")
def chain():
    """按路径加载脚本（`if __name__ == "__main__"` 守护，导入无副作用）。"""
    spec = importlib.util.spec_from_file_location("check_trace_chain_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module            # `@dataclass` 要按 `__module__` 反查得到模块
    spec.loader.exec_module(module)
    return module


def observation(obs_id: str, name: str, *, parent: str = "", type_: str = "SPAN",
                start: str = "2026-10-15T08:00:00.000Z", end: str = "2026-10-15T08:00:01.000Z",
                **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"id": obs_id, "name": name, "type": type_,
                            "parentObservationId": parent or None,
                            "startTime": start, "endTime": end}
    item.update(extra)
    return item


def good_trace() -> dict[str, Any]:
    """一条口径全对的对话 trace：chat.turn → tool → analyze → node → generation。"""
    return {"id": "trace-1", "observations": [
        observation("s1", "chat.turn", start="2026-10-15T08:00:00.000Z",
                    end="2026-10-15T08:00:04.000Z"),
        observation("s2", "tool.run_hotspot_analysis", parent="s1",
                    start="2026-10-15T08:00:00.200Z", end="2026-10-15T08:00:03.800Z"),
        observation("s3", "analyze", parent="s2",
                    start="2026-10-15T08:00:01.000Z", end="2026-10-15T08:00:03.500Z"),
        observation("s4", "node.clue", parent="s3",
                    start="2026-10-15T08:00:01.100Z", end="2026-10-15T08:00:01.900Z"),
        observation("g1", "hotspot_clue", parent="s4", type_="GENERATION",
                    usageDetails={"input": 100, "output": 50, "total": 150},
                    costDetails={"input": 0.0002, "output": 0.0004, "total": 0.0006}),
        observation("g2", "chat_supervisor", parent="s1", type_="GENERATION",
                    start="2026-10-15T08:00:00.100Z", end="2026-10-15T08:00:00.300Z",
                    usageDetails={"input": 300, "output": 60, "total": 360},
                    costDetails={"total": 0.001}),
    ]}


def checks_by_name(checks: list[Any]) -> dict[str, Any]:
    return {check.name: check for check in checks}


class TestVerifyTrace:
    def test_healthy_trace_passes_every_hard_check(self, chain) -> None:
        checks = chain.verify_trace(good_trace(), expected_latency_ms=4000)
        assert all(check.ok is not False for check in checks)
        assert checks_by_name(checks)["工具 span"].ok is True
        assert checks_by_name(checks)["worker 链"].ok is True
        assert checks_by_name(checks)["叶子用量"].ok is True
        assert checks_by_name(checks)["耗时口径"].ok is True

    def test_missing_entry_fails(self, chain) -> None:
        trace = {"observations": [observation("s2", "tool.run_hotspot_analysis")]}
        checks = checks_by_name(chain.verify_trace(trace))
        assert checks["入口 span"].ok is False

    def test_analyze_is_accepted_as_api_style_root(self, chain) -> None:
        trace = {"observations": [observation("s1", "analyze"),
                                  observation("s2", "node.clue", parent="s1")]}
        checks = checks_by_name(chain.verify_trace(trace))
        assert checks["入口 span"].ok is True
        assert checks["worker 链"].ok is None          # analyze 自己没有工具父：跳过

    def test_chat_turn_wins_even_when_analyze_comes_first(self, chain) -> None:
        """云端列表顺序不保证（实测 analyze 会排在 chat.turn 前）：入口要固定优先 chat.turn。"""
        trace = {"observations": [
            observation("s9", "analyze", parent="s8"),
            observation("s7", "chat.turn", parent="s8"),
            observation("s8", "POST /messages"),
            observation("s6", "tool.run_hotspot_analysis", parent="s7"),
            observation("s5", "node.clue", parent="s9"),
        ]}
        checks = checks_by_name(chain.verify_trace(trace))
        assert "对话轮次" in checks["入口 span"].detail
        assert checks["工具 span"].ok is True
        assert checks["worker 链"].ok is False

    def test_tool_outside_root_chain_fails(self, chain) -> None:
        trace = good_trace()
        trace["observations"].append(observation("t9", "tool.other",
                                                 start="2026-10-15T08:00:02.000Z"))
        checks = checks_by_name(chain.verify_trace(trace))
        assert checks["工具 span"].ok is False
        assert "tool.other" in checks["工具 span"].detail

    def test_worker_not_under_tool_fails(self, chain) -> None:
        trace = good_trace()
        trace["observations"].append(observation("w1", "worker.material_sync", parent="s1"))
        checks = checks_by_name(chain.verify_trace(trace))
        assert checks["worker 链"].ok is False
        assert "worker.material_sync" in checks["worker 链"].detail

    def test_generation_without_usage_fails_but_unknown_passes(self, chain) -> None:
        trace = good_trace()
        trace["observations"].append(observation("g3", "material_select", parent="s3",
                                                 type_="GENERATION"))
        checks = checks_by_name(chain.verify_trace(trace))
        assert checks["叶子用量"].ok is False
        assert "material_select" in checks["叶子用量"].detail

        trace["observations"][-1]["metadata"] = {"usage_status": "unknown"}
        checks = checks_by_name(chain.verify_trace(trace))
        assert checks["叶子用量"].ok is True

    def test_latency_outside_tolerance_fails(self, chain) -> None:
        checks = checks_by_name(chain.verify_trace(good_trace(), expected_latency_ms=1000,
                                                   tolerance_ms=500))
        assert checks["耗时口径"].ok is False
        assert "9000" not in checks["耗时口径"].detail      # 只是把差值写清楚，不臆造

    def test_no_worker_round_is_skipped_not_failed(self, chain) -> None:
        trace = {"observations": [observation("s1", "chat.turn"),
                                  observation("g1", "chat_supervisor", parent="s1",
                                              type_="GENERATION",
                                              usageDetails={"total": 10})]}
        checks = checks_by_name(chain.verify_trace(trace))
        assert checks["工具 span"].ok is None
        assert checks["worker 链"].ok is None
        assert chain.verify_trace(trace)[0].ok is True


class TestHelpers:
    def test_tree_lines_indent_by_parent(self, chain) -> None:
        lines = chain.tree_lines(good_trace()["observations"])
        assert lines[0].startswith("- chat.turn")
        assert any(line.startswith("  - tool.run_hotspot_analysis") for line in lines)
        assert any(line.startswith("    - analyze") for line in lines)
        assert any("有用量" in line for line in lines)

    def test_observation_latency_prefers_times_then_falls_back(self, chain) -> None:
        assert chain.observation_latency_ms(observation("a", "x")) == 1000
        assert chain.observation_latency_ms({"latency": 1.5}) == 1500
        assert chain.observation_latency_ms({}) is None

    def test_parse_time_handles_z_and_garbage(self, chain) -> None:
        assert chain.parse_time("2026-10-15T08:00:00.000Z") is not None
        assert chain.parse_time("not-a-time") is None
        assert chain.parse_time(None) is None

    def test_trace_wall_uses_outermost_bounds(self, chain) -> None:
        assert chain.trace_wall_ms(good_trace()["observations"]) == 4000
        assert chain.trace_wall_ms([]) is None
