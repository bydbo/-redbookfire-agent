"""`scripts/check_trace_chain.py` 的纯函数单测（S7.22；R6 / OBS-08 扩充）：离线、不连库、不联网。

覆盖核对口径本身：入口唯一 / 引用完整 / 工具 / worker 链 / 视觉 expect、叶子用量白名单
（unknown 只认 metadata，ERROR 不再自动等于未知）、显式零成本、账本核对（token 精确 +
成本容差 + 失败轮与未知调用的"待验证"）、入口 span 耗时口径，以及 span 树的打印。
云端响应形状用最小字典伪造（只放脚本真正读的字段）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_trace_chain.py"


@pytest.fixture(scope="module")
def chain():
    """按路径加载脚本（`if __name__ == "__main__"` 守护，导入无副作用）。"""
    spec = importlib.util.spec_from_file_location(
        "check_trace_chain_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # `@dataclass` 要按 `__module__` 反查得到模块
    spec.loader.exec_module(module)
    return module


def observation(
    obs_id: str,
    name: str,
    *,
    parent: str = "",
    type_: str = "SPAN",
    start: str = "2026-10-15T08:00:00.000Z",
    end: str = "2026-10-15T08:00:01.000Z",
    **extra: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": obs_id,
        "name": name,
        "type": type_,
        "parentObservationId": parent or None,
        "startTime": start,
        "endTime": end,
    }
    item.update(extra)
    return item


def generation(
    obs_id: str,
    name: str,
    *,
    parent: str,
    prompt: int = 100,
    completion: int = 50,
    cost: float = 0.0006,
    **extra: Any,
) -> dict[str, Any]:
    return observation(
        obs_id,
        name,
        parent=parent,
        type_="GENERATION",
        usageDetails={
            "input": prompt,
            "output": completion,
            "total": prompt + completion,
        },
        costDetails={"total": cost},
        **extra,
    )


def good_trace() -> dict[str, Any]:
    """一条口径全对的对话 trace：chat.turn → tool → analyze → node → generation。"""
    entry = observation(
        "s1",
        "chat.turn",
        start="2026-10-15T08:00:00.000Z",
        end="2026-10-15T08:00:04.000Z",
        metadata={
            "status": "succeeded",
            "prompt_tokens": 400,
            "completion_tokens": 110,
            "cost_cny": 0.0016,
            "usage_complete": True,
            "usage_unknown_calls": 0,
        },
    )
    return {
        "id": "trace-1",
        "observations": [
            entry,
            observation(
                "s2",
                "tool.run_hotspot_analysis",
                parent="s1",
                start="2026-10-15T08:00:00.200Z",
                end="2026-10-15T08:00:03.800Z",
            ),
            observation(
                "s3",
                "analyze",
                parent="s2",
                start="2026-10-15T08:00:01.000Z",
                end="2026-10-15T08:00:03.500Z",
            ),
            observation(
                "s4",
                "node.clue",
                parent="s3",
                start="2026-10-15T08:00:01.100Z",
                end="2026-10-15T08:00:01.900Z",
            ),
            generation("g1", "hotspot_clue", parent="s4"),
            generation(
                "g2",
                "chat_supervisor",
                parent="s1",
                prompt=300,
                completion=60,
                cost=0.001,
                start="2026-10-15T08:00:00.100Z",
                end="2026-10-15T08:00:00.300Z",
            ),
        ],
    }


GOOD_LEDGER = {"prompt_tokens": 400, "completion_tokens": 110, "cost_cny": 0.0016}


def checks_by_name(checks: list[Any]) -> dict[str, Any]:
    return {check.name: check for check in checks}


def verify(chain, trace, **kwargs):
    return checks_by_name(chain.verify_trace(trace, **kwargs))


def all_ok(checks: dict[str, Any], *, strict: bool = False) -> bool:
    items = checks.values()
    return (
        all(check.ok is True for check in items)
        if strict
        else all(check.ok is not False for check in items)
    )


class TestVerifyTrace:
    def test_healthy_trace_passes_every_hard_check(self, chain) -> None:
        checks = verify(
            chain, good_trace(), expected_latency_ms=4000, ledger=GOOD_LEDGER
        )
        assert all_ok(checks)
        assert all_ok(checks, strict=True)  # 全对时连跳过都没有
        assert not any(check.pending for check in checks.values())
        assert checks["工具 span"].ok is True
        assert checks["worker 链"].ok is True
        assert checks["叶子用量"].ok is True
        assert checks["账本核对"].ok is True
        assert checks["耗时口径"].ok is True

    def test_missing_entry_fails(self, chain) -> None:
        trace = {"observations": [observation("s2", "tool.run_hotspot_analysis")]}
        checks = verify(chain, trace)
        assert checks["入口 span"].ok is False

    def test_duplicate_entry_fails(self, chain) -> None:
        """R6 新口径：出现多个 chat.turn（串轮 / 重复上报）必须判失败，不能默默取第一个。"""
        trace = good_trace()
        trace["observations"].append(
            observation("s10", "chat.turn", start="2026-10-15T08:05:00.000Z")
        )
        checks = verify(chain, trace)
        assert checks["入口 span"].ok is False
        assert "2 个 chat.turn" in checks["入口 span"].detail

    def test_orphan_parent_reference_fails(self, chain) -> None:
        """R6 新口径：parent 不在本 trace 内 = 链路残缺，必须暴露而不是默默画树。"""
        trace = good_trace()
        trace["observations"].append(
            observation("x1", "node.clue", parent="missing-id")
        )
        checks = verify(chain, trace)
        assert checks["引用完整"].ok is False
        assert "x1" in checks["引用完整"].detail

    def test_analyze_is_accepted_as_api_style_root(self, chain) -> None:
        trace = {
            "observations": [
                observation("s1", "analyze"),
                observation("s2", "node.clue", parent="s1"),
            ]
        }
        checks = verify(chain, trace)
        assert checks["入口 span"].ok is True
        assert checks["worker 链"].ok is None  # analyze 自己没有工具父：跳过

    def test_chat_turn_wins_even_when_analyze_comes_first(self, chain) -> None:
        """云端列表顺序不保证（实测 analyze 会排在 chat.turn 前）：入口要固定优先 chat.turn。"""
        trace = {
            "observations": [
                observation("s9", "analyze", parent="s8"),
                observation("s7", "chat.turn", parent="s8"),
                observation("s8", "POST /messages"),
                observation("s6", "tool.run_hotspot_analysis", parent="s7"),
                observation("s5", "node.clue", parent="s9"),
            ]
        }
        checks = verify(chain, trace)
        assert "对话轮次" in checks["入口 span"].detail
        assert checks["工具 span"].ok is True
        assert checks["worker 链"].ok is False

    def test_tool_outside_root_chain_fails(self, chain) -> None:
        trace = good_trace()
        trace["observations"].append(
            observation("t9", "tool.other", start="2026-10-15T08:00:02.000Z")
        )
        checks = verify(chain, trace)
        assert checks["工具 span"].ok is False
        assert "tool.other" in checks["工具 span"].detail

    def test_worker_not_under_tool_fails(self, chain) -> None:
        trace = good_trace()
        trace["observations"].append(
            observation("w1", "worker.material_sync", parent="s1")
        )
        checks = verify(chain, trace)
        assert checks["worker 链"].ok is False
        assert "worker.material_sync" in checks["worker 链"].detail

    def test_expect_upgrades_absence_to_failure(self, chain) -> None:
        """--expect 把"没有对应环节"从跳过升级为失败（R6：声明即硬口径）。"""
        plain = {
            "observations": [
                observation("s1", "chat.turn"),
                generation("g1", "chat_supervisor", parent="s1"),
            ]
        }
        assert verify(chain, plain)["工具 span"].ok is None
        assert verify(chain, plain, expect=["tool"])["工具 span"].ok is False
        assert verify(chain, plain, expect=["workers"])["worker 链"].ok is False
        assert verify(chain, plain, expect=["no-vision"])["视觉调用"].ok is True

    def test_expect_vision_requires_vision_generation(self, chain) -> None:
        trace = good_trace()
        checks = verify(chain, trace, expect=["vision"])
        assert checks["视觉调用"].ok is False
        trace["observations"].append(
            generation(
                "v1",
                "image_hotspot_clue",
                parent="s2",
                prompt=500,
                completion=20,
                cost=0.003,
                start="2026-10-15T08:00:01.200Z",
                end="2026-10-15T08:00:01.800Z",
            )
        )
        checks = verify(chain, trace, expect=["vision"])
        assert checks["视觉调用"].ok is True
        assert "image_hotspot_clue" in checks["视觉调用"].detail

    def test_expect_no_vision_fails_when_vision_present(self, chain) -> None:
        trace = good_trace()
        trace["observations"].append(
            generation("v1", "image_hotspot_clue_multi", parent="s2", cost=0.003)
        )
        checks = verify(chain, trace, expect=["no-vision"])
        assert checks["视觉调用"].ok is False
        assert "image_hotspot_clue_multi" in checks["视觉调用"].detail

    def test_generation_without_usage_fails_but_unknown_is_pending(self, chain) -> None:
        """R6 修正：unknown 只认 metadata.usage_status；ERROR 级别不再自动等于未知。"""
        trace = good_trace()
        trace["observations"].append(
            observation("g3", "material_select", parent="s3", type_="GENERATION")
        )
        checks = verify(chain, trace)
        assert checks["叶子用量"].ok is False
        assert "material_select" in checks["叶子用量"].detail

        # ERROR 级别但有真实用量 + 成本：不是未知，也不算缺（R6 修正的核心差异点）
        trace["observations"][-1]["level"] = "ERROR"
        trace["observations"][-1]["usageDetails"] = {"input": 10, "output": 5}
        trace["observations"][-1]["costDetails"] = {"total": 0.0}
        checks = verify(chain, trace)
        assert checks["叶子用量"].ok is True

        # 如实标 unknown：不是失败，而是"待验证"（整脚本退出码 3 的来源）
        trace["observations"][-1] = observation(
            "g3",
            "material_select",
            parent="s3",
            type_="GENERATION",
            metadata={"usage_status": "unknown"},
        )
        checks = verify(chain, trace)
        assert checks["叶子用量"].ok is None
        assert checks["叶子用量"].pending is True

    def test_invalid_usage_values_fail(self, chain) -> None:
        """负数 / 字符串 / bool 这类脏用量必须暴露（R6：usage_values_valid）。"""
        for bad in (
            {"input": -1, "output": 5},
            {"input": "100", "output": 5},
            {"input": True, "output": 5},
        ):
            trace = good_trace()
            trace["observations"].append(
                observation(
                    "g3",
                    "material_select",
                    parent="s3",
                    type_="GENERATION",
                    usageDetails=bad,
                    costDetails={"total": 0.0},
                )
            )
            assert verify(chain, trace)["叶子用量"].ok is False

    def test_explicit_zero_cost_passes(self, chain) -> None:
        """显式 0 成本（免费 / 缓存场景）不算缺成本（R6：has_cost 允许显式零）。"""
        trace = good_trace()
        trace["observations"].append(
            generation(
                "g3", "material_select", parent="s3", prompt=0, completion=0, cost=0
            )
        )
        assert verify(chain, trace)["叶子用量"].ok is True

    def test_ledger_mismatch_fails(self, chain) -> None:
        trace = good_trace()
        checks = verify(
            chain,
            trace,
            ledger={"prompt_tokens": 999, "completion_tokens": 110, "cost_cny": 0.0016},
        )
        assert checks["账本核对"].ok is False
        assert "999" in checks["账本核对"].detail

    def test_ledger_cost_within_tolerance_passes(self, chain) -> None:
        checks = verify(
            chain,
            good_trace(),
            ledger={"prompt_tokens": 400, "completion_tokens": 110, "cost_cny": 0.0017},
        )  # 差 0.0001 ≤ 0.01
        assert checks["账本核对"].ok is True

    def test_ledger_skipped_without_db_row(self, chain) -> None:
        checks = verify(chain, good_trace(), ledger=None)
        assert checks["账本核对"].ok is None
        assert checks["账本核对"].pending is False

    def test_failed_turn_ledger_is_pending_not_failed(self, chain) -> None:
        """失败 / 取消轮只保证已知部分入账：账本核对为"待验证"，不武断判失败（OBS-10）。"""
        trace = good_trace()
        trace["observations"][0]["metadata"]["status"] = "failed"
        checks = verify(chain, trace, ledger=GOOD_LEDGER)
        assert checks["账本核对"].ok is None
        assert checks["账本核对"].pending is True

    def test_unknown_calls_make_ledger_pending(self, chain) -> None:
        """入口 metadata 标了 usage_unknown_calls > 0 → 待验证（R6：从云侧证据读）。"""
        trace = good_trace()
        trace["observations"][0]["metadata"]["usage_unknown_calls"] = 2
        checks = verify(chain, trace, ledger=GOOD_LEDGER)
        assert checks["账本核对"].ok is None
        assert checks["账本核对"].pending is True
        assert "2 个未知调用" in checks["账本核对"].detail

    def test_latency_uses_entry_span_not_wall_clock(self, chain) -> None:
        """耗时口径 = 入口 span 耗时；并行 worker 拉宽的墙钟只作展示参考（R6）。"""
        trace = good_trace()
        # 加一个晚结束的并行 worker：墙钟被拉到 6s，但入口 span 仍是 4s
        trace["observations"].append(
            observation(
                "w1",
                "worker.notify",
                parent="s1",
                start="2026-10-15T08:00:05.000Z",
                end="2026-10-15T08:00:06.000Z",
            )
        )
        checks = verify(chain, trace, expected_latency_ms=4000)
        assert checks["耗时口径"].ok is True
        assert "6000" in checks["耗时口径"].detail  # 墙钟写清楚，仅供参考

        checks = verify(chain, trace, expected_latency_ms=1000, tolerance_ms=500)
        assert checks["耗时口径"].ok is False

    def test_latency_missing_either_side_is_skipped_or_pending(self, chain) -> None:
        no_expected = verify(chain, good_trace())
        assert no_expected["耗时口径"].ok is None
        assert no_expected["耗时口径"].pending is False  # --trace-id 直连：跳过

        trace = good_trace()
        trace["observations"][0].pop("endTime")  # 入口缺起止时间：待验证
        checks = verify(chain, trace, expected_latency_ms=4000)
        assert checks["耗时口径"].ok is None
        assert checks["耗时口径"].pending is True

    def test_no_worker_round_is_skipped_not_failed(self, chain) -> None:
        trace = {
            "observations": [
                observation("s1", "chat.turn"),
                observation(
                    "g1",
                    "chat_supervisor",
                    parent="s1",
                    type_="GENERATION",
                    usageDetails={"total": 10},
                    costDetails={"total": 0.0},
                ),
            ]
        }
        checks = verify(chain, trace)
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

    def test_tree_lines_marks_unknown_and_estimated(self, chain) -> None:
        observations = [
            observation("s1", "chat.turn"),
            observation(
                "g1",
                "a",
                parent="s1",
                type_="GENERATION",
                metadata={"usage_status": "unknown"},
            ),
            observation(
                "g2",
                "b",
                parent="s1",
                type_="GENERATION",
                usageDetails={"input": 1, "output": 2},
                metadata={"usage_status": "estimated"},
            ),
        ]
        lines = chain.tree_lines(observations)
        assert any("用量未知" in line and "- a" in line for line in lines)
        assert any("估算用量" in line and "- b" in line for line in lines)

    def test_usage_status_reads_metadata_only(self, chain) -> None:
        """R6 修正点单测：ERROR 级别不再自动等于 unknown；只认 metadata.usage_status。"""
        assert (
            chain.usage_status_of({"metadata": {"usage_status": "UNKNOWN"}})
            == "unknown"
        )
        assert chain.usage_status_of({"level": "ERROR"}) == ""
        assert chain.usage_status_of({}) == ""

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

    def test_probe_usage_and_cost(self, chain) -> None:
        item = generation("g", "x", parent="p")
        assert chain.probe_usage(item) == ("valid", (100, 50))
        state, value = chain.probe_cost(item)
        assert state == "valid" and abs(float(value) - 0.0006) < 1e-9
        assert chain.probe_usage({}) == ("missing", None)
        assert chain.probe_cost({}) == ("missing", None)
        # R5（E7OBS2-05）：非法形状 / 值一律 invalid，不按零核账
        assert chain.probe_cost({"costDetails": {"total": "bad"}}) == ("invalid", None)
        assert chain.probe_cost({"costDetails": {"total": True}}) == ("invalid", None)
        assert chain.probe_cost({"costDetails": {"total": -1}}) == ("invalid", None)
        assert chain.probe_cost({"costDetails": {"total": float("nan")}}) == (
            "invalid",
            None,
        )
        assert chain.probe_cost({"costDetails": {"total": float("inf")}}) == (
            "invalid",
            None,
        )
        assert chain.probe_cost({"costDetails": {"weird": 1}}) == ("invalid", None)
        # 显式零合法；分项求和合法
        assert chain.probe_cost({"costDetails": {"total": 0.0}}) == ("valid", 0.0)
        state, value = chain.probe_cost({"costDetails": {"input": 0.1, "output": 0.2}})
        assert state == "valid" and abs(float(value) - 0.3) < 1e-9
        assert (
            chain.probe_usage({"usageDetails": {"input": -1, "output": 5}})[0]
            == "invalid"
        )
        assert (
            chain.probe_usage({"usageDetails": {"input": "100", "output": 5}})[0]
            == "invalid"
        )
        assert (
            chain.probe_usage({"usageDetails": {"total": 10}})[0] == "invalid"
        )  # 不支持形状


class TestCostAndUsageParsing:
    """R5（E7OBS2-05）：非法成本不能通过核对；叶子与账本同一解析结果。"""

    def test_invalid_cost_string_fails_leaf_and_ledger(self, chain) -> None:
        trace = good_trace()
        trace["observations"].append(
            generation("g3", "bad_leaf", parent="s4", cost="bad")
        )
        checks = verify(chain, trace, ledger=GOOD_LEDGER)
        assert checks["叶子用量"].ok is False
        assert "非法" in checks["叶子用量"].detail
        assert checks["账本核对"].ok is False  # 不能因"转换失败按 0"而通过
        assert "无法核对" in checks["账本核对"].detail

    def test_invalid_cost_not_hidden_by_unknown_declaration(self, chain) -> None:
        trace = good_trace()
        trace["observations"].append(
            generation(
                "g3",
                "sneaky",
                parent="s4",
                cost=-1,
                metadata={"usage_status": "unknown"},
            )
        )
        checks = verify(chain, trace)
        assert checks["叶子用量"].ok is False  # 无效数字不因 unknown 被隐藏

    def test_cost_without_usage_and_usage_without_cost_both_fail(self, chain) -> None:
        trace = good_trace()
        trace["observations"].append(
            observation(
                "g3",
                "no_usage",
                parent="s4",
                type_="GENERATION",
                costDetails={"total": 0.01},
            )
        )
        trace["observations"].append(
            observation(
                "g4",
                "no_cost",
                parent="s4",
                type_="GENERATION",
                usageDetails={"input": 5, "output": 5},
            )
        )
        checks = verify(chain, trace)
        assert checks["叶子用量"].ok is False
        assert "no_usage" in checks["叶子用量"].detail
        assert "no_cost" in checks["叶子用量"].detail

    def test_explicit_zero_value_still_valid(self, chain) -> None:
        trace = good_trace()
        trace["observations"].append(
            generation("g3", "free_call", parent="s4", prompt=0, completion=0, cost=0)
        )
        checks = verify(chain, trace)
        assert checks["叶子用量"].ok is True

    def test_latency_prefers_same_basis_window(self, chain) -> None:
        """有轮次时间窗时按**同边界**比对（入口 span 只作参考）；不一致照样判失败。"""
        trace = good_trace()
        trace["observations"][0]["metadata"].update(
            {
                "run_started_at": "2026-10-15T08:00:00.000Z",
                "run_finished_at": "2026-10-15T08:00:03.000Z",
            }
        )  # 窗 3000ms，入口 span 4000ms
        checks = verify(chain, trace, expected_latency_ms=3000)
        assert checks["耗时口径"].ok is True
        assert "同边界" in checks["耗时口径"].detail

        checks = verify(chain, trace, expected_latency_ms=4000, tolerance_ms=500)
        assert checks["耗时口径"].ok is False  # 3000 vs 4000：按同边界就不一致

    def test_ledger_accepts_decimal_cost_from_db(self, chain) -> None:
        """真机回放抓到的漏网：库里的 cost_cny 是 Decimal，不能被"只认 int/float"误判为无效。"""
        from decimal import Decimal

        ledger = {
            "prompt_tokens": 400,
            "completion_tokens": 110,
            "cost_cny": Decimal("0.0016"),
        }
        checks = verify(chain, good_trace(), ledger=ledger)
        assert checks["账本核对"].ok is True

    def test_json_cli_stdout_is_pure_json(
        self, chain, capsys, monkeypatch, tmp_path
    ) -> None:
        class Cfg:
            langfuse_keys = ("pk", "sk", "http://host")

        monkeypatch.setattr(chain, "load_config", lambda: Cfg())
        monkeypatch.setattr(
            chain, "load_trace", lambda cfg, tid, retries=2: good_trace()
        )
        save = tmp_path / "trace.json"
        code = chain.main(["--trace-id", "t1", "--json", "--save", str(save)])

        out, err = capsys.readouterr()
        payload = json.loads(out)  # stdout 可直接解析
        assert payload["trace_id"] == "t1"
        assert payload["checks"]
        assert "[已存证]" in err  # 说明挪到 stderr
        assert out.count("{") >= 1 and "[已存证]" not in out
        assert code == 0

    def test_json_cli_failure_exit_code_still_reported(
        self, chain, capsys, monkeypatch
    ) -> None:
        class Cfg:
            langfuse_keys = ("pk", "sk", "http://host")

        monkeypatch.setattr(chain, "load_config", lambda: Cfg())
        broken = good_trace()
        broken["observations"][0]["metadata"]["usage_unknown_calls"] = 1
        monkeypatch.setattr(chain, "load_trace", lambda cfg, tid, retries=2: broken)
        code = chain.main(["--trace-id", "t2", "--json", "--expect", "vision"])

        out, _err = capsys.readouterr()
        payload = json.loads(out)
        assert code == 1
        assert payload["passed"] is False
        assert any(item["name"] == "视觉调用" for item in payload["failures"])


class TestCli:
    def test_parse_args_expect_and_retries(self, chain) -> None:
        args = chain.parse_args(
            ["--turn-id", "t1", "--expect", "tool,vision", "--json"]
        )
        assert args.expect == "tool,vision"
        assert args.retries == 2 and args.as_json is True

    def test_main_rejects_bad_expect(self, chain, capsys) -> None:
        assert chain.main(["--trace-id", "x", "--expect", "bogus"]) == 2
        assert "bogus" in capsys.readouterr().out

    def test_main_rejects_missing_turn(self, chain, capsys, monkeypatch) -> None:
        async def fake_lookup(cfg, turn_id):
            return None

        monkeypatch.setattr(chain, "_lookup_turn", fake_lookup)
        monkeypatch.setattr(chain, "load_config", lambda: object())
        assert chain.main(["--turn-id", "nope"]) == 2
