"""启动前置检查的契约测试（S1.2）。

用注入的 EnvView 构造「缺失」与「齐全」两种情形，不读真实 config/.env。
第 3–7 步（DB / 扩展 / 迁移 / Redis / dist）的用例用**不可达端点与临时目录**，离线可跑。
"""

from __future__ import annotations

import socket

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from xhs_agent.config import AppConfig, EnvView, load_config
from xhs_agent.probe import (
    CURRENT_STAGE,
    E_CONFIG_INVALID,
    E_CONFIG_MISSING,
    E_DB_UNAVAILABLE,
    E_FRONTEND_DIST_MISSING,
    E_REDIS_UNAVAILABLE,
    PreflightReport,
    Problem,
    check_database,
    check_frontend,
    check_redis,
    main,
    preflight,
    run_startup_checks,
)


def make_config(tmp_path, body: str = "") -> AppConfig:
    """写一份临时 config.toml 并加载。

    `[embedding].enabled` 的默认值是 `true`（S2.5 起，见配置契约 §3.3），因此除显式测试
    embedding 开关的用例外，本模块统一先把它关掉——否则每个用例都得额外带 DASHSCOPE_API_KEY，
    掩盖了各自的测试意图。需要测向量的用例自己传 `[embedding]` 段。
    """
    if "[embedding]" not in body:
        body = "[embedding]\nenabled = false\n" + body
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return load_config(str(path))


def view(**values: str) -> EnvView:
    return EnvView({}, values)


DSN = "postgresql+asyncpg://xhs:xhs@localhost:5432/xhs"
REDIS_DSN = "redis://localhost:6379/0"


def names(report) -> list[str]:
    return [problem.name for problem in report.problems]


def stub_dependency_checks(monkeypatch) -> None:
    """把第 3–7 步全部 stub 掉：用例不去连真实库 / Redis / 目录。"""
    from xhs_agent import probe as probe_module

    async def _no_problem_async(*_args, **_kwargs):
        return None

    for name in ("check_database", "check_extensions", "check_migration", "check_redis"):
        monkeypatch.setattr(probe_module, name, _no_problem_async)
    # check_frontend 是同步函数（纯判断），单独 stub
    monkeypatch.setattr(probe_module, "check_frontend", lambda *_a, **_k: None)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def ready_cfg(tmp_path, **env_values: str) -> AppConfig:
    """第 1–2 步能过的配置：注入完整 env（不读真实 config/.env）。"""
    cfg = make_config(tmp_path)
    values = {"DEEPSEEK_API_KEY": "sk-x", "DATABASE_URL": DSN, "REDIS_URL": REDIS_DSN}
    values.update(env_values)
    cfg._env = EnvView({}, values)
    return cfg


class TestRequiredVariables:
    def test_complete_env_passes(self, tmp_path):
        report = preflight(make_config(tmp_path),
                           env=view(DEEPSEEK_API_KEY="sk-x", DATABASE_URL=DSN,
                                    REDIS_URL=REDIS_DSN))
        assert report.ok
        assert report.exit_code == 0
        assert report.problems == []

    def test_missing_text_model_key_blocks(self, tmp_path):
        report = preflight(make_config(tmp_path), env=view(), stage="P1")
        assert report.exit_code == 2
        assert names(report) == ["DEEPSEEK_API_KEY"]
        assert report.problems[0].code == E_CONFIG_MISSING

    def test_fix_hint_is_actionable(self, tmp_path):
        report = preflight(make_config(tmp_path), env=view())
        fix = report.problems[0].fix
        assert ".env" in fix and "config/.env.example" in fix

    def test_required_name_follows_config(self, tmp_path):
        cfg = make_config(tmp_path, '[llm]\napi_key_env = "MY_LLM_KEY"\n')
        assert names(preflight(cfg, env=view(), stage="P1")) == ["MY_LLM_KEY"]

    def test_explicit_override_satisfies_requirement(self, tmp_path):
        report = preflight(make_config(tmp_path), env=view(XHS_LLM_API_KEY="sk-explicit"),
                           stage="P1")
        assert report.ok

    def test_blank_value_counts_as_missing(self, tmp_path):
        report = preflight(make_config(tmp_path), env=view(DEEPSEEK_API_KEY="   "))
        assert not report.ok


class TestCapabilityGradedRequirements:
    def test_disabled_capabilities_do_not_require_dashscope(self, tmp_path):
        report = preflight(make_config(tmp_path), env=view(DEEPSEEK_API_KEY="sk-x"))
        assert "DASHSCOPE_API_KEY" not in names(report)

    def test_vision_enabled_requires_dashscope(self, tmp_path):
        cfg = make_config(tmp_path, "[vision]\nenabled = true\n")
        report = preflight(cfg, env=view(DEEPSEEK_API_KEY="sk-x"), stage="P1")
        assert names(report) == ["DASHSCOPE_API_KEY"]

    def test_embedding_enabled_requires_dashscope(self, tmp_path):
        cfg = make_config(tmp_path, "[embedding]\nenabled = true\n")
        report = preflight(cfg, env=view(DEEPSEEK_API_KEY="sk-x"), stage="P1")
        assert names(report) == ["DASHSCOPE_API_KEY"]

    def test_both_capabilities_report_one_problem(self, tmp_path):
        cfg = make_config(tmp_path, "[vision]\nenabled = true\n[embedding]\nenabled = true\n")
        report = preflight(cfg, env=view(DEEPSEEK_API_KEY="sk-x"), stage="P1")
        assert names(report) == ["DASHSCOPE_API_KEY"]

    def test_capability_key_present_passes(self, tmp_path):
        cfg = make_config(tmp_path, "[vision]\nenabled = true\n")
        report = preflight(cfg, env=view(DEEPSEEK_API_KEY="sk-x", DASHSCOPE_API_KEY="sk-v"),
                           stage="P1")
        assert report.ok


class TestStagedRequirements:
    def test_p2_variables_are_not_required_at_p1(self, tmp_path):
        report = preflight(make_config(tmp_path), env=view(DEEPSEEK_API_KEY="sk-x"), stage="P1")
        assert report.ok
        assert "DATABASE_URL" not in names(report)
        assert "REDIS_URL" not in names(report)

    def test_malformed_p2_variable_does_not_block_at_p1(self, tmp_path):
        env = view(DEEPSEEK_API_KEY="sk-x", DATABASE_URL="postgres://wrong-scheme")
        assert preflight(make_config(tmp_path), env=env, stage="P1").ok

    def test_p2_stage_requires_database_url(self, tmp_path):
        report = preflight(make_config(tmp_path), env=view(DEEPSEEK_API_KEY="sk-x"), stage="P2")
        assert report.exit_code == 2
        assert names(report) == ["DATABASE_URL"]
        assert report.problems[0].code == E_CONFIG_MISSING

    def test_p2_stage_rejects_wrong_scheme(self, tmp_path):
        env = view(DEEPSEEK_API_KEY="sk-x", DATABASE_URL="postgres://user:pw@db/xhs")
        report = preflight(make_config(tmp_path), env=env, stage="P2")
        assert report.problems[0].code == E_CONFIG_INVALID

    def test_p2_stage_accepts_asyncpg_dsn(self, tmp_path):
        env = view(DEEPSEEK_API_KEY="sk-x",
                   DATABASE_URL="postgresql+asyncpg://xhs:pw@db:5432/xhs")
        assert preflight(make_config(tmp_path), env=env, stage="P2").ok

    def test_p3_stage_requires_redis_url(self, tmp_path):
        env = view(DEEPSEEK_API_KEY="sk-x",
                   DATABASE_URL="postgresql+asyncpg://xhs:pw@db:5432/xhs")
        report = preflight(make_config(tmp_path), env=env, stage="P3")
        assert names(report) == ["REDIS_URL"]

    def test_redis_accepts_tls_scheme(self, tmp_path):
        env = view(DEEPSEEK_API_KEY="sk-x",
                   DATABASE_URL="postgresql+asyncpg://xhs:pw@db:5432/xhs",
                   REDIS_URL="rediss://cache:6379/0")
        assert preflight(make_config(tmp_path), env=env, stage="P3").ok


class TestObservabilityWarning:
    def test_partial_langfuse_only_warns(self, tmp_path):
        env = view(DEEPSEEK_API_KEY="sk-x", LANGFUSE_PUBLIC_KEY="pk")
        report = preflight(make_config(tmp_path), env=env, stage="P1")
        assert report.ok
        assert len(report.warnings) == 1
        assert "LANGFUSE_SECRET_KEY" in report.warnings[0]
        assert "LANGFUSE_HOST" in report.warnings[0]

    def test_complete_langfuse_is_silent(self, tmp_path):
        env = view(DEEPSEEK_API_KEY="sk-x", LANGFUSE_PUBLIC_KEY="pk",
                   LANGFUSE_SECRET_KEY="sk", LANGFUSE_HOST="https://cloud.langfuse.com")
        report = preflight(make_config(tmp_path), env=env, stage="P1")
        assert report.ok and report.warnings == []


class TestCommandLine:
    def test_main_returns_zero_when_ready(self, tmp_path, clean_contract_env, capsys,
                                          monkeypatch):
        clean_contract_env.setenv("XHS_CONFIG_PATH", str(tmp_path / "config.toml"))
        (tmp_path / "config.toml").write_text("[embedding]\nenabled = false\n", encoding="utf-8")
        clean_contract_env.setenv("DEEPSEEK_API_KEY", "sk-test-value")
        clean_contract_env.setenv("DATABASE_URL", DSN)
        clean_contract_env.setenv("REDIS_URL", REDIS_DSN)
        stub_dependency_checks(monkeypatch)      # 第 3–7 步的实查在集成用例里做
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "配置摘要（已脱敏）" in out
        assert "前置检查通过" in out
        assert "sk-test-value" not in out

    def test_main_returns_two_and_prints_fix_hint(self, tmp_path, clean_contract_env, capsys):
        clean_contract_env.setenv("XHS_CONFIG_PATH", str(tmp_path / "config.toml"))
        (tmp_path / "config.toml").write_text("", encoding="utf-8")
        assert main([]) == 2
        out = capsys.readouterr().out
        assert f"{E_CONFIG_MISSING}: DEEPSEEK_API_KEY" in out
        assert "修复：" in out

    def test_main_reports_invalid_config(self, tmp_path, clean_contract_env, capsys):
        path = tmp_path / "config.toml"
        path.write_text("[match]\ntopk = 0\n", encoding="utf-8")
        clean_contract_env.setenv("XHS_CONFIG_PATH", str(path))
        assert main([]) == 2
        out = capsys.readouterr().out
        assert f"{E_CONFIG_INVALID}:" in out
        assert "topk" in out

    def test_main_help(self, capsys):
        assert main(["--help"]) == 0
        assert "退出码" in capsys.readouterr().out

    def test_main_rejects_unknown_argument(self, capsys):
        assert main(["--nope"]) == 2
        assert "未知参数" in capsys.readouterr().err

    def test_current_stage_is_p3(self):
        # S3.4b 落地 Celery 队列后进入 P3：REDIS_URL 变为必填
        assert CURRENT_STAGE == "P3"

    def test_report_defaults_to_current_stage(self, tmp_path):
        report = preflight(make_config(tmp_path), env=view(DEEPSEEK_API_KEY="sk-x"))
        assert report.stage == CURRENT_STAGE


class TestExitCodes:
    """退出码由**首个问题的 kind** 推出：config → 2、dependency → 3、无问题 → 0。"""

    def test_no_problem_is_zero(self):
        assert PreflightReport().exit_code == 0

    def test_config_problem_is_two(self):
        report = PreflightReport(problems=[Problem(E_CONFIG_MISSING, "X", "m", "f", step=1)])
        assert report.exit_code == 2

    def test_dependency_problem_is_three(self):
        report = PreflightReport(problems=[
            Problem(E_REDIS_UNAVAILABLE, "X", "m", "f", step=6, kind="dependency")])
        assert report.exit_code == 3

    def test_first_problem_decides(self):
        report = PreflightReport(problems=[
            Problem(E_FRONTEND_DIST_MISSING, "A", "m", "f", step=7),
            Problem(E_REDIS_UNAVAILABLE, "B", "m", "f", step=6, kind="dependency"),
        ])
        assert report.exit_code == 2


class TestDependencyChecks:
    """第 3–7 步：用不可达端点与临时目录，完全不碰真实依赖。"""

    @pytest.mark.asyncio
    async def test_database_unavailable_is_masked_dependency_problem(self, tmp_path):
        dsn = "postgresql+asyncpg://xhs:xhs@127.0.0.1:1/xhs"
        cfg = ready_cfg(tmp_path, DATABASE_URL=dsn)
        engine = create_async_engine(dsn, poolclass=NullPool)
        try:
            problem = await check_database(cfg, engine)
        finally:
            await engine.dispose()
        assert problem is not None
        assert (problem.code, problem.step, problem.kind) == (E_DB_UNAVAILABLE, 3, "dependency")
        assert "xhs:xhs@" not in problem.message        # 密码已脱敏
        assert "***" in problem.message

    @pytest.mark.asyncio
    async def test_redis_unavailable_is_dependency_problem(self, tmp_path):
        cfg = ready_cfg(tmp_path, REDIS_URL=f"redis://127.0.0.1:{free_port()}/0")
        problem = await check_redis(cfg)
        assert problem is not None
        assert (problem.code, problem.step, problem.kind) == (E_REDIS_UNAVAILABLE, 6, "dependency")

    def test_missing_dist_is_config_problem(self, tmp_path):
        cfg = make_config(tmp_path, '[frontend]\nserve = true\ndist_dir = "nowhere/dist"\n')
        problem = check_frontend(cfg)
        assert problem is not None
        assert (problem.code, problem.step, problem.kind) == (E_FRONTEND_DIST_MISSING, 7, "config")
        assert "pnpm build" in problem.fix              # 修复提示要能照做

    def test_serve_disabled_skips_dist_check(self, tmp_path):
        cfg = make_config(tmp_path, '[frontend]\nserve = false\ndist_dir = "nowhere/dist"\n')
        assert check_frontend(cfg) is None

    def test_existing_dist_passes(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("x", encoding="utf-8")
        cfg = make_config(tmp_path,
                          f'[frontend]\nserve = true\ndist_dir = "{dist.as_posix()}"\n')
        assert check_frontend(cfg) is None


class TestRunStartupChecks:
    @pytest.mark.asyncio
    async def test_stops_at_the_first_failure(self, tmp_path, monkeypatch):
        """第 3 步失败就不再往下查（Redis / dist 一律不碰）。"""
        from xhs_agent import probe as probe_module

        cfg = ready_cfg(tmp_path, DATABASE_URL="postgresql+asyncpg://xhs:xhs@127.0.0.1:1/xhs")
        touched: list[str] = []
        monkeypatch.setattr(probe_module, "check_redis",
                            lambda *_a, **_k: (touched.append("redis"), None)[1])
        monkeypatch.setattr(probe_module, "check_frontend",
                            lambda *_a, **_k: (touched.append("frontend"), None)[1])

        report = await run_startup_checks(cfg)

        assert [problem.code for problem in report.problems] == [E_DB_UNAVAILABLE]
        assert report.exit_code == 3
        assert touched == []

    @pytest.mark.asyncio
    async def test_config_problems_short_circuit_before_io(self, tmp_path, monkeypatch):
        """第 1–2 步没过时连数据库都不该碰。"""
        from xhs_agent import probe as probe_module

        called: list[str] = []
        monkeypatch.setattr(probe_module, "check_database",
                            lambda *_a, **_k: called.append("db"))

        report = await run_startup_checks(make_config(tmp_path))   # 缺三个必填项

        assert not report.ok
        assert report.exit_code == 2
        assert called == []
