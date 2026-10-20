"""E8 管理入口 plan/apply/verify 在临时库的空库路径与篡改拒绝。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def empty_head(db_engine: AsyncEngine, postgres_dsn: str,
                    monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    await asyncio.to_thread(
        command.upgrade, Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\nmaterials_dir = "{(tmp_path / "materials").as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        f'index_dir = "{(tmp_path / "runs" / "_index").as_posix()}"\n',
        encoding="utf-8")
    env = {**os.environ, "DATABASE_URL": postgres_dsn,
           "XHS_CONFIG_PATH": str(cfg), "PYTHONIOENCODING": "utf-8"}
    return env, tmp_path


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "e8_snapshot.py"), *args],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")


async def test_cli_plan_apply_verify_and_tamper(empty_head) -> None:
    env, tmp_path = empty_head
    plan_path = tmp_path / "plan.json"
    planned = _run(env, "plan", "--output", str(plan_path))
    assert planned.returncode == 0, planned.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["counts"]["runs"] == 0 and plan["executable"] is True

    applied = _run(env, "apply", "--plan", str(plan_path))
    assert applied.returncode == 0, applied.stderr
    verified = _run(env, "verify", "--batch", plan["batch_id"])
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["status"] == "verified"

    tampered = json.loads(plan_path.read_text(encoding="utf-8"))
    tampered["counts"]["runs"] = 99
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(tampered), encoding="utf-8")
    rejected = _run(env, "apply", "--plan", str(bad))
    assert rejected.returncode == 1
