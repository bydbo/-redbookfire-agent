"""配置加载的契约测试（S1.2）。

覆盖三层优先级、字段校验（未知字段/类型/范围）、密钥解析与脱敏。
所有用例只读 tmp_path 下的临时 TOML / .env，不碰真实的 config/。
"""

from __future__ import annotations

import json
import os

import pytest

from xhs_agent.config import (PROJECT_ROOT, AppConfig, ConfigError, EnvView,
                              config_to_dict, load_config)


def write_config(tmp_path, body: str) -> str:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def write_dotenv(tmp_path, body: str) -> None:
    (tmp_path / ".env").write_text(body, encoding="utf-8")


class TestLayerPrecedence:
    def test_code_defaults_apply_to_unspecified_fields(self, tmp_path):
        cfg = load_config(write_config(tmp_path, '[llm]\nmodel = "custom-model"\n'))
        assert cfg.llm.model == "custom-model"
        assert cfg.llm.provider == "openai_compatible"
        assert cfg.match.topk == 5
        assert cfg.vision.enabled is False

    def test_toml_overrides_code_defaults(self, tmp_path):
        cfg = load_config(write_config(tmp_path, "[match]\ntopk = 3\n"))
        assert cfg.match.topk == 3

    def test_dotenv_overrides_toml(self, tmp_path):
        write_dotenv(tmp_path, "XHS_LLM_MODEL=from-dotenv\n")
        cfg = load_config(write_config(tmp_path, '[llm]\nmodel = "from-toml"\n'))
        assert cfg.llm.model == "from-dotenv"

    def test_environment_overrides_dotenv_and_toml(self, tmp_path, clean_contract_env):
        write_dotenv(tmp_path, "XHS_LLM_MODEL=from-dotenv\n")
        clean_contract_env.setenv("XHS_LLM_MODEL", "from-env")
        cfg = load_config(write_config(tmp_path, '[llm]\nmodel = "from-toml"\n'))
        assert cfg.llm.model == "from-env"

    def test_empty_override_does_not_wipe_value(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv("XHS_LLM_MODEL", "")
        cfg = load_config(write_config(tmp_path, '[llm]\nmodel = "from-toml"\n'))
        assert cfg.llm.model == "from-toml"

    def test_flat_overrides_are_normalized(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv("XHS_LOG_LEVEL", "debug")
        clean_contract_env.setenv("XHS_LLM_PROVIDER", "deepseek")
        cfg = load_config(write_config(tmp_path, ""))
        assert cfg.log_level == "DEBUG"
        assert cfg.llm.provider == "deepseek"

    def test_xhs_config_path_selects_another_file(self, tmp_path, clean_contract_env):
        path = write_config(tmp_path, '[llm]\nmodel = "from-env-path"\n')
        clean_contract_env.setenv("XHS_CONFIG_PATH", path)
        cfg = load_config()
        assert cfg.llm.model == "from-env-path"
        assert cfg.config_path == os.path.abspath(path)

    def test_node_project_root_is_used_for_relative_paths(self, tmp_path):
        cfg = load_config(write_config(tmp_path, ""))
        assert cfg.materials_dir() == os.path.join(PROJECT_ROOT, "data", "materials")
        assert cfg.runs_dir() == os.path.join(PROJECT_ROOT, "runs")
        assert cfg.index_dir() == os.path.join(PROJECT_ROOT, "runs", "_index")


class TestValidation:
    @pytest.mark.parametrize("body, expected", [
        ('[llm]\nmodel_name = "typo"\n', "llm.model_name"),
        ("[unknown]\nfoo = 1\n", "unknown"),
        ("[match]\ntopk = 0\n", "match.topk"),
        ("[match]\nmin_score = 2\n", "match.min_score"),
        ("[retrieval]\nrecall_limit = 501\n", "retrieval.recall_limit"),
        ("[embedding]\ndim = 768\n", "embedding.dim"),
        ('[llm]\nprovider = "auto"\n', "llm.provider"),
        ('[llm]\nprovider = "offline"\n', "llm.provider"),
        ('[llm]\ntimeout_s = "abc"\n', "llm.timeout_s"),
        ("[queue]\ntask_soft_time_limit_s = 600\ntask_time_limit_s = 100\n", "task_time_limit_s"),
        ('log_level = "TRACE"\n', "log_level"),
        ('[llm]\napi_key = "sk-should-not-be-here"\n', "llm.api_key"),
        ('[vision]\napi_key = "sk-should-not-be-here"\n', "vision.api_key"),
    ])
    def test_invalid_config_is_rejected(self, tmp_path, body, expected):
        with pytest.raises(ConfigError) as info:
            load_config(write_config(tmp_path, body))
        assert expected in str(info.value)

    def test_retrieval_weights_must_sum_to_one(self, tmp_path):
        with pytest.raises(ConfigError) as info:
            load_config(write_config(tmp_path, "[retrieval]\nw_element = 0.5\n"))
        assert "w_element" in str(info.value)

    def test_toml_syntax_error_is_reported(self, tmp_path):
        with pytest.raises(ConfigError) as info:
            load_config(write_config(tmp_path, "[llm\nmodel = "))
        assert "解析失败" in str(info.value)
        assert info.value.fix

    def test_missing_explicit_path_is_reported(self, tmp_path):
        with pytest.raises(ConfigError) as info:
            load_config(str(tmp_path / "nope.toml"))
        assert "不存在" in str(info.value)

    def test_invalid_flat_env_override_is_rejected(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv("XHS_LLM_PROVIDER", "offline")
        with pytest.raises(ConfigError) as info:
            load_config(write_config(tmp_path, ""))
        assert "llm.provider" in str(info.value)

    def test_config_error_carries_invalid_code(self, tmp_path):
        with pytest.raises(ConfigError) as info:
            load_config(write_config(tmp_path, "[match]\ntopk = 0\n"))
        assert info.value.code == "E_CONFIG_INVALID"
        assert info.value.fix

class TestKeyResolution:
    def test_provider_key_is_read_from_dotenv(self, tmp_path):
        write_dotenv(tmp_path, "DEEPSEEK_API_KEY=sk-from-dotenv\n")
        cfg = load_config(write_config(tmp_path, ""))
        assert cfg.llm.resolved_key() == "sk-from-dotenv"

    def test_process_env_wins_over_dotenv(self, tmp_path, clean_contract_env):
        write_dotenv(tmp_path, "DEEPSEEK_API_KEY=sk-from-dotenv\n")
        clean_contract_env.setenv("DEEPSEEK_API_KEY", "sk-from-env")
        assert load_config(write_config(tmp_path, "")).llm.resolved_key() == "sk-from-env"

    def test_explicit_override_wins_over_provider_key(self, tmp_path):
        write_dotenv(tmp_path,
                     "DEEPSEEK_API_KEY=sk-provider\nXHS_LLM_API_KEY=sk-explicit\n")
        assert load_config(write_config(tmp_path, "")).llm.resolved_key() == "sk-explicit"

    def test_api_key_env_can_be_renamed(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv("MY_LLM_KEY", "sk-renamed")
        cfg = load_config(write_config(tmp_path, '[llm]\napi_key_env = "MY_LLM_KEY"\n'))
        assert cfg.llm.resolved_key() == "sk-renamed"

    def test_vision_key_follows_its_own_env_name(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv("MY_VISION_KEY", "sk-vision")
        cfg = load_config(write_config(
            tmp_path, '[vision]\nenabled = true\napi_key_env = "MY_VISION_KEY"\n'))
        assert cfg.vision.resolved_key() == "sk-vision"

    def test_dotenv_is_not_injected_into_process_env(self, tmp_path):
        write_dotenv(tmp_path, "DEEPSEEK_API_KEY=sk-from-dotenv\n")
        load_config(write_config(tmp_path, ""))
        assert "DEEPSEEK_API_KEY" not in os.environ


class TestRedaction:
    def test_snapshots_do_not_leak_provider_key(self, tmp_path):
        write_dotenv(tmp_path, "DEEPSEEK_API_KEY=sk-secret-value\n")
        cfg = load_config(write_config(tmp_path, ""))
        for snapshot in (config_to_dict(cfg), cfg.describe()):
            assert "sk-secret-value" not in json.dumps(snapshot, ensure_ascii=False)

    def test_describe_reports_presence_only(self, tmp_path):
        write_dotenv(tmp_path, "DEEPSEEK_API_KEY=sk-secret-value\n")
        cfg = load_config(write_config(tmp_path, ""))
        assert cfg.describe()["llm"]["has_api_key"] is True
        assert "key" in cfg.describe()["llm"]["api_key_env"].lower()

    def test_langfuse_secret_is_masked(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv("LANGFUSE_SECRET_KEY", "lf-secret")
        cfg = load_config(write_config(tmp_path, ""))
        assert config_to_dict(cfg)["langfuse_secret_key"] == "***"
        assert "lf-secret" not in json.dumps(config_to_dict(cfg), ensure_ascii=False)

    def test_langfuse_is_env_only(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config(write_config(tmp_path, 'langfuse_secret_key = "lf-secret"\n'))

    def test_dsn_password_is_masked(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv("DATABASE_URL", "postgresql+asyncpg://xhs:s3cret@db:5432/xhs")
        clean_contract_env.setenv("REDIS_URL", "redis://:r3dis@cache:6379/0")
        described = load_config(write_config(tmp_path, "")).describe()
        assert described["database_url"] == "postgresql+asyncpg://xhs:***@db:5432/xhs"
        assert described["redis_url"] == "redis://:***@cache:6379/0"
        assert "s3cret" not in json.dumps(described, ensure_ascii=False)
        assert "r3dis" not in json.dumps(described, ensure_ascii=False)


class TestAppConfigSurface:
    def test_env_view_defaults_to_empty(self):
        assert EnvView().get("ANYTHING") == ""
        assert not EnvView().has("ANYTHING")

    def test_env_view_prefers_process_env(self):
        view = EnvView({"KEY": "from-dotenv"}, {"KEY": "from-env"})
        assert view.get("KEY") == "from-env"

    def test_defaults_match_contract(self):
        cfg = AppConfig()
        assert cfg.llm.provider == "openai_compatible"
        assert cfg.llm.api_key_env == "DEEPSEEK_API_KEY"
        assert cfg.vision.enabled is False
        assert cfg.embedding.enabled is False
        assert cfg.embedding.dim == 1024
        assert cfg.retrieval.w_element + cfg.retrieval.w_rrf == pytest.approx(1.0)
        assert cfg.frontend.serve is True
