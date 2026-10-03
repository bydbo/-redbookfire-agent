"""启动包装的单测（S3.8）：离线，不打真实依赖、不起服务器。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from xhs_agent import serve
from xhs_agent.config import PROJECT_ROOT, ConfigError, load_config
from xhs_agent.probe import (
    E_CONFIG_MISSING,
    E_FRONTEND_DIST_MISSING,
    E_REDIS_UNAVAILABLE,
    PreflightReport,
    Problem,
)


def make_config(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    return load_config(str(path))


def async_return(value):
    async def _call(*_args, **_kwargs):
        return value
    return _call


class TestParseArgs:
    def test_defaults(self):
        args = serve.parse_args([])
        assert (args.host, args.port) == ("127.0.0.1", 8000)

    def test_overrides(self):
        args = serve.parse_args(["--host", "0.0.0.0", "--port", "9000"])
        assert (args.host, args.port) == ("0.0.0.0", 9000)


class TestMain:
    def _prepare(self, monkeypatch, tmp_path, report):
        """把配置、前置检查与 uvicorn 全部替换掉，返回被记录的启动调用。"""
        started: list = []
        monkeypatch.setattr(serve, "load_config", lambda: make_config(tmp_path))
        monkeypatch.setattr(serve, "run_startup_checks", async_return(report))
        monkeypatch.setattr(serve.uvicorn, "run", lambda *a, **k: started.append((a, k)))
        return started

    def test_dependency_failure_returns_three_and_never_starts(
            self, monkeypatch, tmp_path, capsys):
        report = PreflightReport(problems=[
            Problem(E_REDIS_UNAVAILABLE, "REDIS_URL", "连不上", "起 Redis",
                    step=6, kind="dependency")])
        started = self._prepare(monkeypatch, tmp_path, report)

        assert serve.main([]) == 3
        assert started == []                       # 检查没过，绝不能起服务器
        err = capsys.readouterr().err
        assert "启动前置检查未通过" in err and "E_REDIS_UNAVAILABLE" in err

    def test_config_failure_returns_two(self, monkeypatch, tmp_path):
        report = PreflightReport(problems=[
            Problem(E_FRONTEND_DIST_MISSING, "frontend.dist_dir", "缺 dist", "pnpm build",
                    step=7)])
        self._prepare(monkeypatch, tmp_path, report)
        assert serve.main([]) == 2

    def test_success_starts_uvicorn_without_double_checking(self, monkeypatch, tmp_path):
        started = self._prepare(monkeypatch, tmp_path, PreflightReport())

        assert serve.main([]) == 0
        assert len(started) == 1
        (app,), kwargs = started[0]
        assert app.state.check_startup is False    # 已经查过，不让 lifespan 再查第二遍
        assert (kwargs["host"], kwargs["port"]) == ("127.0.0.1", 8000)

    def test_invalid_config_returns_two(self, monkeypatch, tmp_path, capsys):
        def boom():
            raise ConfigError("配置坏了", "按提示修")

        started: list = []
        monkeypatch.setattr(serve, "load_config", boom)
        monkeypatch.setattr(serve.uvicorn, "run", lambda *a, **k: started.append(a))

        assert serve.main([]) == 2
        assert started == []
        assert "E_CONFIG_INVALID" in capsys.readouterr().err

    def test_config_problem_exit_code_is_used_directly(self, monkeypatch, tmp_path):
        report = PreflightReport(problems=[
            Problem(E_CONFIG_MISSING, "DEEPSEEK_API_KEY", "缺密钥", "写进 config/.env", step=1)])
        self._prepare(monkeypatch, tmp_path, report)
        assert serve.main([]) == 2


class TestSubprocessExitCodes:
    """真跑一次子进程，确认 2/3 都在**绑定端口之前**就返回（离线：不连真实依赖）。"""

    @staticmethod
    def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "xhs_agent.serve"], cwd=str(PROJECT_ROOT),
                              env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)

    def test_missing_required_env_exits_two(self, tmp_path, clean_contract_env):
        """缺必填项（第 1 步）→ 退出码 2。"""
        config_path = tmp_path / "config.toml"
        config_path.write_text("", encoding="utf-8")
        env = dict(os.environ)          # clean_contract_env 已经把契约变量清干净
        env["XHS_CONFIG_PATH"] = str(config_path)
        env["PYTHONIOENCODING"] = "utf-8"

        result = self._run(env)

        assert result.returncode == 2
        assert "E_CONFIG_MISSING" in result.stderr

    def test_unreachable_database_exits_three(self, tmp_path, clean_contract_env):
        """配置齐全但数据库连不上（第 3 步）→ 退出码 3。"""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[embedding]\nenabled = false\n", encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "XHS_CONFIG_PATH": str(config_path),
            "PYTHONIOENCODING": "utf-8",
            "DEEPSEEK_API_KEY": "sk-test-value",
            "DATABASE_URL": "postgresql+asyncpg://xhs:xhs@127.0.0.1:1/xhs",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
        })

        result = self._run(env)

        assert result.returncode == 3
        assert "E_DB_UNAVAILABLE" in result.stderr
        assert "sk-test-value" not in result.stderr      # 摘要脱敏
