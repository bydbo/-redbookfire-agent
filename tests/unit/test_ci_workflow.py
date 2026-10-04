"""CI workflow 的静态校验（S4.5；S5.2 起含 frontend job）。

真实验收是"推送 main 后 GitHub Actions 的 Python 三关、镜像 job 与前端契约校验 job 全绿"
（见 `docs/backlog.md` 的 S4.5 / S5.2）；这里守住几条改 workflow 时最容易悄悄坏掉的回归：
少了一关、action 没钉版本、混进密钥、覆盖率门槛与 CI 跑法不匹配、
前端类型生成物的防漂移校验缺席。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"

# 三个 Python job 与它们的命令（与 README / docs/开发规范.md 的手写口径逐条一致）
PYTHON_JOBS = {
    "lint": "uv run ruff check .",
    "typecheck": "uv run mypy",
    "test": "uv run pytest -q --cov --cov-report=term-missing",
}
UV_VERSION = "0.12.7"
PYTHON_VERSION = "3.12"
COVERAGE_GATE = 78
# frontend job（S5.2）：Node / pnpm 版本必须与 frontend/package.json 的
# engines / packageManager 以及 frontend/.node-version 一致
FRONTEND_DIR = "frontend"
FRONTEND_NODE_VERSION = "25.3.0"
PNPM_VERSION = "11.25.0"


def load_workflow() -> dict[str, Any]:
    """解析 workflow；注意 YAML 1.1 会把裸 `on` 解析成布尔 True。"""
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    if "on" not in data and True in data:
        data["on"] = data[True]
    return data


def job_commands(job: dict[str, Any]) -> list[str]:
    return [str(step["run"]) for step in job["steps"] if "run" in step]


def job_uses(job: dict[str, Any]) -> list[str]:
    return [str(step["uses"]) for step in job["steps"] if "uses" in step]


def setup_uv_input(job: dict[str, Any]) -> dict[str, Any]:
    for step in job["steps"]:
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
            return dict(step.get("with") or {})
    raise AssertionError("该 job 没有用 astral-sh/setup-uv")


def step_inputs(job: dict[str, Any], uses_prefix: str) -> dict[str, Any]:
    for step in job["steps"]:
        if str(step.get("uses", "")).startswith(uses_prefix):
            return dict(step.get("with") or {})
    raise AssertionError(f"该 job 没有用 {uses_prefix}")


class TestTriggers:
    def test_workflow_exists_and_parses(self):
        assert WORKFLOW.is_file()
        assert load_workflow()["name"] == "CI"

    def test_runs_on_push_pr_and_manual(self):
        triggers = load_workflow()["on"]
        assert triggers["push"]["branches"] == ["main"]
        assert "pull_request" in triggers
        assert "workflow_dispatch" in triggers

    def test_cancels_superseded_runs(self):
        concurrency = load_workflow()["concurrency"]
        assert concurrency["cancel-in-progress"] is True
        assert "github.ref" in concurrency["group"]


class TestJobs:
    def test_has_python_gates_image_and_frontend(self):
        assert set(load_workflow()["jobs"]) == {"lint", "typecheck", "test", "image", "frontend"}

    def test_python_jobs_pin_actions_python_and_uv(self):
        for name, command in PYTHON_JOBS.items():
            job = load_workflow()["jobs"][name]
            uses = job_uses(job)
            assert any(ref.startswith("actions/checkout@") for ref in uses), name
            inputs = setup_uv_input(job)
            assert inputs["version"] == UV_VERSION, name
            assert inputs["python-version"] == PYTHON_VERSION, name
            assert inputs["enable-cache"] is True, name
            assert inputs["cache-dependency-glob"] == "uv.lock", name
            commands = job_commands(job)
            assert "uv sync" in commands, name
            assert command in commands, name

    def test_test_job_runs_integration_suite(self):
        commands = job_commands(load_workflow()["jobs"]["test"])
        assert "uv run pytest -m integration -q" in commands

    def test_image_job_validates_compose_and_builds(self):
        job = load_workflow()["jobs"]["image"]
        commands = job_commands(job)
        assert "docker compose config --quiet" in commands
        assert "docker build -t xhs-agent:ci ." in commands
        assert job_uses(job) == ["actions/checkout@v7.0.1"]     # 镜像 job 不需要 Python

    def test_frontend_job_pins_node_and_pnpm_and_checks_schema(self):
        """S5.2：Node / pnpm 版本钉死（与 frontend/package.json 一致），
        跑防漂移校验（重新生成 schema.d.ts 并 git diff，契约改了类型没重生成就红）。"""
        job = load_workflow()["jobs"]["frontend"]
        assert job_uses(job) == [
            "actions/checkout@v7.0.1",
            "pnpm/action-setup@v6.1.0",
            "actions/setup-node@v7.0.0",
        ]
        pnpm_inputs = step_inputs(job, "pnpm/action-setup@")
        assert pnpm_inputs["version"] == PNPM_VERSION
        assert pnpm_inputs["run_install"] is False
        node_inputs = step_inputs(job, "actions/setup-node@")
        assert node_inputs["node-version"] == FRONTEND_NODE_VERSION
        assert node_inputs["cache"] == "pnpm"
        assert node_inputs["cache-dependency-path"] == f"{FRONTEND_DIR}/pnpm-lock.yaml"
        commands = job_commands(job)
        assert "pnpm install --frozen-lockfile" in commands
        assert "pnpm run check:api" in commands
        run_steps = [step for step in job["steps"] if "run" in step]
        assert all(step.get("working-directory") == FRONTEND_DIR for step in run_steps)

    def test_uv_frozen_is_set_for_every_job(self):
        assert load_workflow()["env"]["UV_FROZEN"] == "1"


class TestSafety:
    def test_no_secrets_are_referenced(self):
        """CI 只跑离线用例——workflow 里出现 secrets 就说明有人把要密钥的东西塞进来了。"""
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "secrets." not in text
        assert "env_file" not in text

    def test_actions_are_pinned_to_concrete_versions(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("uses: "):
                ref = stripped.split("uses: ", 1)[1].strip()
                assert "@" in ref, ref
                assert ref.split("@", 1)[1] not in {"main", "master", "latest"}, ref

    def test_coverage_gate_matches_pyproject(self):
        """门槛数字与 CI 的跑法必须成对：CI 用 --cov，pyproject 给 fail_under。"""
        with PYPROJECT.open("rb") as fh:
            config = tomllib.load(fh)
        assert config["tool"]["coverage"]["report"]["fail_under"] == COVERAGE_GATE
        assert any("--cov" in command for command in job_commands(load_workflow()["jobs"]["test"]))
