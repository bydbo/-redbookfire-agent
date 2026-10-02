"""模型调用层单测：全部用假 provider，不联网、不读密钥。"""

from __future__ import annotations

import importlib
import inspect

import pytest

from xhs_agent.config import LLMConfig
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMError, LLMResult, StructuredCaller


class FakeProvider(llm.BaseProvider):
    """按预设文本依次返回，用来观察重试行为。"""

    name = "fake"

    def __init__(self, texts):
        super().__init__(model="fake-1")
        self.texts = list(texts)
        self.prompts = []

    def complete(self, call):
        self.prompts.append(call.user)
        text = self.texts.pop(0) if self.texts else ""
        return LLMResult(text=text, provider=self.name, model=self.model)


class TestEstimateTokens:
    def test_empty_text_has_floor_of_one(self):
        assert llm.estimate_tokens("") == 1

    def test_roughly_one_and_a_half_chars_per_token(self):
        assert llm.estimate_tokens("abcde") == 3
        assert llm.estimate_tokens("a" * 150) == 100


class TestLLMResult:
    def test_record_shape(self):
        result = LLMResult(text="x", provider="fake", model="m", prompt_tokens=10,
                           completion_tokens=5, latency_ms=100, cost_cny=0.00123)
        record = result.record("hotspot_clue")
        assert record == {
            "task": "hotspot_clue",
            "provider": "fake",
            "model": "m",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "latency_ms": 100,
            "cost_cny": 0.0012,
            "attempts": 1,
            "error": "",
        }

    def test_label_uses_model_name(self):
        assert FakeProvider(["{}"]).label == "fake:fake-1"


class TestStructuredCaller:
    def test_parses_json_on_first_try(self):
        caller = StructuredCaller(provider=FakeProvider(['{"value": 7}']))
        parsed, result = caller.call("task", "system", "user", parse=lambda data: data["value"])
        assert parsed == 7
        assert result.text == '{"value": 7}'
        assert len(caller.records) == 1

    def test_repairs_by_feeding_error_back(self):
        provider = FakeProvider(["不是 JSON", '{"value": 7}'])
        caller = StructuredCaller(provider=provider)
        parsed, _result = caller.call("task", "system", "user", parse=lambda data: data["value"])
        assert parsed == 7
        assert len(provider.prompts) == 2
        assert "上一次输出无法被解析" in provider.prompts[1]

    def test_raises_after_repair_budget_is_used_up(self):
        caller = StructuredCaller(provider=FakeProvider(["坏输出", "还是坏输出"]))
        with pytest.raises(LLMError):
            caller.call("task", "system", "user", parse=lambda data: data["value"])

    def test_provider_error_is_surfaced(self):
        class FailingProvider(FakeProvider):
            def complete(self, call):
                return LLMResult(text="", provider=self.name, error="连接失败")

        caller = StructuredCaller(provider=FailingProvider([]))
        with pytest.raises(LLMError) as info:
            caller.call("task", "system", "user", parse=lambda data: data)
        assert "连接失败" in str(info.value)


class TestBuildProvider:
    @pytest.mark.parametrize("name", ["openai_compatible", "openai", "deepseek", "qwen",
                                      "dashscope", "compatible"])
    def test_compatible_aliases_share_one_implementation(self, name):
        provider = llm.build_provider(FakeConfig(provider=name))
        assert isinstance(provider, llm.OpenAICompatibleProvider)

    def test_unknown_provider_is_rejected(self):
        # provider 的合法值由配置层校验（S1.2），这里用 model_construct 绕过校验，
        # 验证工具层自己的兜底分支仍然存在。
        with pytest.raises(LLMError):
            llm.build_provider(FakeConfig(provider="bogus"))

    def test_empty_base_url_is_rejected(self):
        with pytest.raises(LLMError):
            llm.build_provider(FakeConfig(base_url=""))


class TestOfflinePathIsGone:
    """S2.7：运行时降级链路已整条删除，这里把"不许复活"钉成可回归的断言。"""

    def test_offline_module_is_removed(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("xhs_agent.tools.offline")

    def test_offline_provider_class_is_removed(self):
        assert not hasattr(llm, "OfflineProvider")

    def test_build_provider_has_no_force_offline_parameter(self):
        assert "force_offline" not in inspect.signature(llm.build_provider).parameters

    def test_offline_provider_name_is_rejected(self):
        with pytest.raises(LLMError):
            llm.build_provider(FakeConfig(provider="offline"))


class TestExtractMessageText:
    def test_plain_content(self):
        assert llm._extract_message_text({"choices": [{"message": {"content": "你好"}}]}) == "你好"

    def test_list_content_is_joined(self):
        payload = {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}
        assert llm._extract_message_text(payload) == "ab"

    def test_missing_choices_raises(self):
        with pytest.raises(LLMError):
            llm._extract_message_text({})

    def test_empty_content_raises(self):
        with pytest.raises(LLMError):
            llm._extract_message_text({"choices": [{"message": {"content": ""}}]})


class FakeConfig:
    """只需要 provider / base_url / model 三个字段，避免测试依赖真实密钥。"""

    def __init__(self, **llm_overrides) -> None:
        # model_construct 跳过配置层校验，用来单独验证工具层的兜底分支
        fields = {"provider": "openai_compatible", "base_url": "https://example.com",
                  "model": "deepseek-flash", "api_key_env": "DEEPSEEK_API_KEY"}
        fields.update(llm_overrides)
        self.llm = LLMConfig.model_construct(**fields)
