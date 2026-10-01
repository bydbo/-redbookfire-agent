"""启动前置检查的契约测试（S1.2）。

用注入的 EnvView 构造「缺失」与「齐全」两种情形，不读真实 config/.env。
"""

from __future__ import annotations

from xhs_agent.config import AppConfig, EnvView, load_config
from xhs_agent.probe import (
    CURRENT_STAGE,
    E_CONFIG_INVALID,
    E_CONFIG_MISSING,
    main,
    preflight,
)


def make_config(tmp_path, body: str = "") -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return load_config(str(path))


def view(**values: str) -> EnvView:
    return EnvView({}, values)


DSN = "postgresql+asyncpg://xhs:xhs@localhost:5432/xhs"


def names(report) -> list[str]:
    return [problem.name for problem in report.problems]


class TestRequiredVariables:
    def test_complete_env_passes(self, tmp_path):
        report = preflight(make_config(tmp_path),
                           env=view(DEEPSEEK_API_KEY="sk-x", DATABASE_URL=DSN))
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
    def test_main_returns_zero_when_ready(self, tmp_path, clean_contract_env, capsys):
        clean_contract_env.setenv("XHS_CONFIG_PATH", str(tmp_path / "config.toml"))
        (tmp_path / "config.toml").write_text("", encoding="utf-8")
        clean_contract_env.setenv("DEEPSEEK_API_KEY", "sk-test-value")
        clean_contract_env.setenv("DATABASE_URL", DSN)
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

    def test_current_stage_is_p2(self):
        assert CURRENT_STAGE == "P2"

    def test_report_defaults_to_current_stage(self, tmp_path):
        report = preflight(make_config(tmp_path), env=view(DEEPSEEK_API_KEY="sk-x"))
        assert report.stage == CURRENT_STAGE
