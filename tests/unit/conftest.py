"""单元测试的公共夹具。

配置相关用例绝不能读到开发者本机的真实 `config/.env` 或环境变量，否则在别人机器上必红。
这里统一清空契约里出现过的变量名，用例再按需通过 monkeypatch 注入。
"""

from __future__ import annotations

import pytest

CONTRACT_ENV_NAMES = (
    "XHS_CONFIG_PATH",
    "XHS_LOG_LEVEL",
    "XHS_LLM_PROVIDER",
    "XHS_LLM_BASE_URL",
    "XHS_LLM_MODEL",
    "XHS_LLM_API_KEY",
    "XHS_EMBEDDING_MODEL",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_HOST",
)


@pytest.fixture(autouse=True)
def clean_contract_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """清空契约里出现过的环境变量，返回 monkeypatch 供用例注入。"""
    for name in CONTRACT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch
