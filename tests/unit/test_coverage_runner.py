"""覆盖率门槛不能掩盖测试失败或混入历史执行的数据。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def runner(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/check_coverage.py"
    spec = importlib.util.spec_from_file_location("coverage_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "OUTPUT", tmp_path)
    return module


@pytest.mark.parametrize("failure_at", [0, 1])
def test_failed_suite_never_produces_combined_success(runner, monkeypatch, failure_at):
    calls = []

    def run(*args, env):
        calls.append(args)
        return 1 if len(calls) - 1 == failure_at else 0

    monkeypatch.setattr(runner, "run", run)
    assert runner.main() == 1
    assert len(calls) == failure_at + 1
    assert not any(args[0] == "coverage" for args in calls)


def test_only_current_suite_files_are_combined_and_gate_failure_propagates(runner, monkeypatch):
    (runner.OUTPUT / ".coverage.unrelated").write_text("stale")
    calls = []

    def run(*args, env):
        calls.append(args)
        return 2 if args[:2] == ("coverage", "report") else 0

    monkeypatch.setattr(runner, "run", run)
    assert runner.main() == 2
    combine = next(args for args in calls if args[:2] == ("coverage", "combine"))
    assert set(combine[3:]) == {str(runner.OUTPUT / ".coverage.unit"),
                               str(runner.OUTPUT / ".coverage.integration")}
    assert "--append" not in combine
