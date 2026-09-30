"""配置加载：config/config.toml + config/.env，环境变量可覆盖。"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import asdict, dataclass, field
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.toml")

# 环境变量优先级最高，方便临时切模型而不用改文件
ENV_OVERRIDES = {
    "XHS_LLM_PROVIDER": "llm.provider",
    "XHS_LLM_BASE_URL": "llm.base_url",
    "XHS_LLM_MODEL": "llm.model",
    "XHS_LLM_API_KEY": "llm.api_key",
}


@dataclass
class LLMConfig:
    provider: str = "auto"  # auto | openai_compatible | offline
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-flash"
    api_key: str = ""
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.6
    max_tokens: int = 2000
    timeout_s: int = 60
    max_retries: int = 2
    # 成本估算单价（人民币元/百万 token）＝ DeepSeek 高价时段单价（$0.3 / $1.2）× 汇率 7.2
    price_in_per_m: float = 2.16
    price_out_per_m: float = 8.64

    def resolved_key(self) -> str:
        return (self.api_key or os.environ.get(self.api_key_env, "")).strip()

    def resolved_provider(self) -> str:
        """provider=auto 时：有 key 走在线模型，没有就降级到离线规则引擎。"""
        prov = (self.provider or "auto").strip().lower()
        if prov != "auto":
            return prov
        return "openai_compatible" if self.resolved_key() else "offline"


@dataclass
class VisionConfig:
    enabled: bool = True
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen-vl-max"
    api_key: str = ""
    api_key_env: str = "DASHSCOPE_API_KEY"
    max_frames: int = 3
    max_width: int = 720

    def resolved_key(self) -> str:
        return (self.api_key or os.environ.get(self.api_key_env, "")).strip()

    def available(self) -> bool:
        return bool(self.enabled and self.resolved_key() and shutil.which("ffmpeg"))


@dataclass
class MatchConfig:
    topk: int = 5
    min_score: float = 0.15
    element_type_weights: dict = field(default_factory=dict)


@dataclass
class PathsConfig:
    materials_dir: str = "data/materials"
    runs_dir: str = "runs"
    index_dir: str = "runs/_index"

    def resolved(self, path: str) -> str:
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(PROJECT_ROOT, path))


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    match: MatchConfig = field(default_factory=MatchConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    config_path: str = DEFAULT_CONFIG_PATH

    def materials_dir(self) -> str:
        return self.paths.resolved(self.paths.materials_dir)

    def runs_dir(self) -> str:
        return self.paths.resolved(self.paths.runs_dir)

    def index_dir(self) -> str:
        return self.paths.resolved(self.paths.index_dir)

    def describe(self) -> dict:
        """给报告用的配置快照，不泄露密钥。"""
        provider = self.llm.resolved_provider()
        return {
            "config_path": self.config_path,
            "llm": {
                "provider": provider,
                "model": self.llm.model if provider != "offline" else "内置规则引擎（无 AI 调用）",
                "base_url": self.llm.base_url if provider != "offline" else "",
                "has_api_key": bool(self.llm.resolved_key()),
            },
            "vision": {
                "enabled": self.vision.enabled,
                "available": self.vision.available(),
                "model": self.vision.model,
            },
            "materials_dir": self.materials_dir(),
            "runs_dir": self.runs_dir(),
        }


def _load_dotenv(path: str) -> dict:
    """极简 .env 解析，不引入额外依赖。"""
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key:
                values[key] = val
    return values


def _set_dotted(cfg: Any, dotted: str, value: Any) -> None:
    target, parts = cfg, dotted.split(".")
    for part in parts[:-1]:
        target = getattr(target, part)
    current = getattr(target, parts[-1], None)
    if isinstance(current, bool):
        value = str(value).strip().lower() in {"1", "true", "yes", "on"}
    elif isinstance(current, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
    elif isinstance(current, float):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
    setattr(target, parts[-1], value)


def _apply_section(cfg: Any, section: dict) -> None:
    for key, value in section.items():
        if not hasattr(cfg, key):
            continue
        current = getattr(cfg, key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged = dict(current)
            merged.update(value)
            setattr(cfg, key, merged)
        else:
            setattr(cfg, key, value)


def load_config(path: str | None = None, provider_override: str | None = None) -> AppConfig:
    """读取配置。文件缺失时用内置默认值，保证开箱可跑。"""
    cfg = AppConfig()
    cfg.config_path = os.path.abspath(path or DEFAULT_CONFIG_PATH)

    if os.path.exists(cfg.config_path):
        with open(cfg.config_path, "rb") as fh:
            data = tomllib.load(fh)
        for name in ("llm", "vision", "match", "paths"):
            section = data.get(name)
            if isinstance(section, dict):
                _apply_section(getattr(cfg, name), section)

    env_values = _load_dotenv(os.path.join(os.path.dirname(cfg.config_path), ".env"))
    for key, val in env_values.items():
        os.environ.setdefault(key, val)
    for key, dotted in ENV_OVERRIDES.items():
        if key in env_values:
            _set_dotted(cfg, dotted, env_values[key])

    for env_key, dotted in ENV_OVERRIDES.items():
        if os.environ.get(env_key):
            _set_dotted(cfg, dotted, os.environ[env_key])

    if provider_override:
        cfg.llm.provider = provider_override

    return cfg


def config_to_dict(cfg: AppConfig) -> dict:
    return asdict(cfg)