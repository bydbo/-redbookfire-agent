"""配置加载：config/config.toml + config/.env，环境变量可覆盖。

三层优先级（契约 `docs/contracts/配置契约.md` 第一节）：

    环境变量  >  config/.env  >  config/config.toml  >  代码内默认值

分工：

- `config.toml` 的段结构、字段类型与取值范围由 Pydantic 模型校验；未知段、未知字段、
  类型或取值越界一律折成 `ConfigError`（`E_CONFIG_INVALID`），由 `probe.py` 的启动
  前置检查转成退出码 2。
- 环境变量与 `.env` 两层交给 pydantic-settings（`_FlatOverrides`）：字段名就是契约里的
  扁平变量名（`XHS_LLM_MODEL` 等）。扁平名要落到嵌套段上，所以这里用
  「扁平覆盖层 + 嵌套配置模型」的组合，而不是把两者塞进同一个 Settings。
- 密钥不进模型：`config.toml` 写 `api_key` 会被 `extra="forbid"` 直接拒绝；需要密钥时
  按 `[llm].api_key_env` 这类字段名从 `EnvView`（环境变量 > `.env`）动态解析。
"""

from __future__ import annotations

import os
import shutil
import tomllib
from collections.abc import Mapping
from typing import Any

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.toml")

# OpenAI 兼容端点的合法 provider 别名；auto / offline 的降级语义已取消（ADR 0001）。
LLM_PROVIDERS = ("openai_compatible", "openai", "deepseek", "qwen", "dashscope", "compatible")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# 环境变量覆盖表：扁平变量名 -> 配置里的位置（契约 §2.2 的可选项）。
# 密钥类变量不在这里——它们由 EnvView 按 api_key_env 动态解析。
FLAT_OVERRIDE_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("xhs_llm_provider", ("llm", "provider")),
    ("xhs_llm_base_url", ("llm", "base_url")),
    ("xhs_llm_model", ("llm", "model")),
    ("xhs_embedding_model", ("embedding", "model")),
    ("xhs_log_level", ("log_level",)),
)


class ConfigError(ValueError):
    """配置不合法：文件缺失、TOML 语法、字段类型/范围、未知字段。"""

    code = "E_CONFIG_INVALID"

    def __init__(self, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix


class EnvView:
    """环境变量与 `.env` 的只读视图，用于按变量名动态查密钥。

    优先级与契约一致：进程环境变量 > `config/.env`。
    """

    def __init__(self, dotenv: Mapping[str, Any] | None = None,
                 environ: Mapping[str, Any] | None = None) -> None:
        values: dict[str, str] = {}
        for key, value in dict(dotenv or {}).items():
            if value is not None:
                values[str(key)] = str(value)
        for key, value in dict(environ or {}).items():
            if value is not None:
                values[str(key)] = str(value)
        self._values = values

    def get(self, name: str) -> str:
        return str(self._values.get(name) or "").strip()

    def has(self, name: str) -> bool:
        return bool(self.get(name))


class _Section(BaseModel):
    """配置段基类：未知字段直接报错，避免拼错的字段被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class LLMConfig(_Section):
    """文本模型。默认值 / 取值范围见《配置契约》§3.1。"""

    provider: str = "openai_compatible"
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-flash"
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = Field(0.6, ge=0, le=2)
    max_tokens: int = Field(2000, ge=1, le=32768)
    timeout_s: int = Field(60, ge=1, le=600)
    max_retries: int = Field(2, ge=0, le=10)
    # 成本估算单价（人民币元/百万 token）＝ DeepSeek 高价时段单价（$0.3 / $1.2）× 汇率 7.2
    price_in_per_m: float = Field(2.16, ge=0)
    price_out_per_m: float = Field(8.64, ge=0)

    _env: EnvView = PrivateAttr(default_factory=EnvView)

    @field_validator("provider", mode="before")
    @classmethod
    def _norm_provider(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text not in LLM_PROVIDERS:
            raise ValueError("provider 只能是 " + " / ".join(LLM_PROVIDERS)
                             + "；auto 与 offline 的降级语义已取消")
        return text

    @field_validator("base_url", "model", "api_key_env", mode="before")
    @classmethod
    def _non_empty(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("不能为空")
        return text

    def resolved_key(self) -> str:
        """文本模型密钥：`XHS_LLM_API_KEY` > `api_key_env` 指向的变量。"""
        return self._env.get("XHS_LLM_API_KEY") or self._env.get(self.api_key_env)

    def resolved_provider(self) -> str:
        """provider 已统一为 OpenAI 兼容实现，不再有 auto / offline 分支。"""
        return self.provider


class VisionConfig(_Section):
    """多模态打标。默认值 / 取值范围见《配置契约》§3.2。"""

    enabled: bool = False
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen-vl-max"
    api_key_env: str = "DASHSCOPE_API_KEY"
    max_frames: int = Field(3, ge=1, le=10)
    max_width: int = Field(720, ge=64, le=4096)

    _env: EnvView = PrivateAttr(default_factory=EnvView)

    @field_validator("base_url", "model", "api_key_env", mode="before")
    @classmethod
    def _non_empty(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("不能为空")
        return text

    def resolved_key(self) -> str:
        return self._env.get(self.api_key_env)

    def available(self) -> bool:
        """能力可用性：开关打开 + 有密钥 + 本机有 ffmpeg（缺 ffmpeg 属能力裁剪）。"""
        return bool(self.enabled and self.resolved_key() and shutil.which("ffmpeg"))


class EmbeddingConfig(_Section):
    """文本向量化。默认值 / 取值范围见《配置契约》§3.3。"""

    enabled: bool = True
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "text-embedding-v3"
    dim: int = 1024
    batch_size: int = Field(10, ge=1, le=128)
    timeout_s: int = Field(60, ge=1, le=600)
    max_retries: int = Field(2, ge=0, le=10)
    api_key_env: str = "DASHSCOPE_API_KEY"

    _env: EnvView = PrivateAttr(default_factory=EnvView)

    @field_validator("base_url", "model", "api_key_env", mode="before")
    @classmethod
    def _non_empty(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("不能为空")
        return text

    @field_validator("dim")
    @classmethod
    def _dim_must_match_schema(cls, value: int) -> int:
        if value != 1024:
            raise ValueError("dim 必须为 1024，与数据契约里 materials.embedding 的维度一致")
        return value

    def resolved_key(self) -> str:
        return self._env.get(self.api_key_env)


class RetrievalConfig(_Section):
    """混合召回参数。默认值 / 取值范围见《配置契约》§3.4 与《检索契约》§8。"""

    similarity_threshold: float = Field(0.2, ge=0, le=1)
    recall_limit: int = Field(50, ge=1, le=500)
    max_cosine_distance: float = Field(0.35, ge=0, le=2)
    rrf_k: int = Field(60, ge=1, le=1000)
    w_element: float = Field(0.7, ge=0, le=1)
    w_rrf: float = Field(0.3, ge=0, le=1)
    hit_threshold: float = Field(0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> RetrievalConfig:
        if abs(self.w_element + self.w_rrf - 1.0) > 1e-6:
            raise ValueError("w_element 与 w_rrf 之和必须为 1")
        return self


class MatchConfig(_Section):
    """打分与截断。默认值 / 取值范围见《配置契约》§3.5。"""

    topk: int = Field(5, ge=1, le=20)
    min_score: float = Field(0.15, ge=0, le=1)
    element_type_weights: dict[str, float] = Field(default_factory=dict)

    @field_validator("element_type_weights", mode="before")
    @classmethod
    def _check_type_weights(cls, value: Any) -> dict[str, float]:
        out: dict[str, float] = {}
        for key, item in dict(value or {}).items():
            try:
                num = float(item)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"要素权重 {key} 不是数字") from exc
            if not 0.0 <= num <= 1.0:
                raise ValueError(f"要素权重 {key} 必须在 0~1 之间")
            out[str(key)] = num
        return out


class PathsConfig(_Section):
    """目录。默认值 / 取值范围见《配置契约》§3.6。"""

    materials_dir: str = "data/materials"
    runs_dir: str = "runs"
    index_dir: str = "runs/_index"

    @field_validator("materials_dir", "runs_dir", "index_dir", mode="before")
    @classmethod
    def _non_empty(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("路径不能为空")
        return text

    def resolved(self, path: str) -> str:
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(PROJECT_ROOT, path))


class DatabaseConfig(_Section):
    """数据库连接池。默认值 / 取值范围见《配置契约》§3.7。"""

    pool_size: int = Field(5, ge=1, le=100)
    max_overflow: int = Field(10, ge=0, le=100)
    echo: bool = False


class QueueConfig(_Section):
    """任务队列。默认值 / 取值范围见《配置契约》§3.8。"""

    task_soft_time_limit_s: int = Field(600, ge=1, le=86400)
    task_time_limit_s: int = Field(900, ge=1, le=86400)
    max_retries: int = Field(2, ge=0, le=10)

    @model_validator(mode="after")
    def _hard_limit_not_below_soft(self) -> QueueConfig:
        if self.task_time_limit_s < self.task_soft_time_limit_s:
            raise ValueError("task_time_limit_s 必须 >= task_soft_time_limit_s")
        return self


class FrontendConfig(_Section):
    """前端静态资源。默认值 / 取值范围见《配置契约》§3.9。"""

    serve: bool = True
    dist_dir: str = "frontend/dist"
    dev_server_url: str = "http://localhost:5173"

    @field_validator("dist_dir", "dev_server_url", mode="before")
    @classmethod
    def _non_empty(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("不能为空")
        return text


class AppConfig(_Section):
    """工程配置全量视图：9 个段与契约 §3 一一对应。"""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    match: MatchConfig = Field(default_factory=MatchConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    frontend: FrontendConfig = Field(default_factory=FrontendConfig)
    log_level: str = "INFO"

    _env: EnvView = PrivateAttr(default_factory=EnvView)
    _config_path: str = PrivateAttr(default=DEFAULT_CONFIG_PATH)

    @field_validator("log_level", mode="before")
    @classmethod
    def _norm_log_level(cls, value: Any) -> str:
        text = str(value or "INFO").strip().upper()
        if text not in LOG_LEVELS:
            raise ValueError("log_level 只能是 " + " / ".join(LOG_LEVELS))
        return text

    @property
    def config_path(self) -> str:
        return self._config_path

    @property
    def env_view(self) -> EnvView:
        return self._env

    @property
    def langfuse_keys(self) -> tuple[str, str, str]:
        """可观测三件套：只从环境变量 / `.env` 读，不进 config.toml。"""
        return (self._env.get("LANGFUSE_PUBLIC_KEY"),
                self._env.get("LANGFUSE_SECRET_KEY"),
                self._env.get("LANGFUSE_HOST"))

    def materials_dir(self) -> str:
        return self.paths.resolved(self.paths.materials_dir)

    def runs_dir(self) -> str:
        return self.paths.resolved(self.paths.runs_dir)

    def index_dir(self) -> str:
        return self.paths.resolved(self.paths.index_dir)

    def describe(self) -> dict[str, Any]:
        """脱敏配置摘要：只出现布尔值与掩码，可安全写进启动日志。"""
        public, secret, host = self.langfuse_keys
        return {
            "config_path": self.config_path,
            "log_level": self.log_level,
            "llm": {
                "provider": self.llm.provider,
                "model": self.llm.model,
                "base_url": self.llm.base_url,
                "api_key_env": self.llm.api_key_env,
                "has_api_key": bool(self.llm.resolved_key()),
            },
            "vision": {
                "enabled": self.vision.enabled,
                "model": self.vision.model,
                "api_key_env": self.vision.api_key_env,
                "has_api_key": bool(self.vision.resolved_key()),
                "ready": self.vision.available(),
            },
            "embedding": {
                "enabled": self.embedding.enabled,
                "model": self.embedding.model,
                "dim": self.embedding.dim,
                "api_key_env": self.embedding.api_key_env,
                "has_api_key": bool(self.embedding.resolved_key()),
            },
            "retrieval": {
                "recall_limit": self.retrieval.recall_limit,
                "hit_threshold": self.retrieval.hit_threshold,
            },
            "match": {"topk": self.match.topk, "min_score": self.match.min_score},
            "paths": {
                "materials_dir": self.materials_dir(),
                "runs_dir": self.runs_dir(),
                "index_dir": self.index_dir(),
            },
            "database_url": _mask_dsn(self._env.get("DATABASE_URL")),
            "redis_url": _mask_dsn(self._env.get("REDIS_URL")),
            "langfuse": {
                "configured": bool(public and secret and host),
                "has_public_key": bool(public),
                "has_secret_key": bool(secret),
                "has_host": bool(host),
            },
        }


class _FlatOverrides(BaseSettings):
    """环境变量 / `.env` 覆盖层：字段就是契约 §2.2 的扁平变量名。

    由 pydantic-settings 决定「环境变量 > `.env`」；这里只把值收上来，
    语义校验交给合并后的 `AppConfig`（所以字段都用宽松的 str 类型）。
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

    xhs_llm_provider: str = Field("", validation_alias="XHS_LLM_PROVIDER")
    xhs_llm_base_url: str = Field("", validation_alias="XHS_LLM_BASE_URL")
    xhs_llm_model: str = Field("", validation_alias="XHS_LLM_MODEL")
    xhs_embedding_model: str = Field("", validation_alias="XHS_EMBEDDING_MODEL")
    xhs_log_level: str = Field("", validation_alias="XHS_LOG_LEVEL")


def _mask_dsn(value: str) -> str:
    """DSN 脱敏：保留协议、用户名与主机，密码替换为 ***。"""
    text = str(value or "").strip()
    if not text:
        return ""
    scheme, sep, rest = text.partition("://")
    if not sep:
        return "***"
    creds, at, host = rest.rpartition("@")
    if not at:
        return text
    user, colon, _pwd = creds.partition(":")
    auth = f"{user}:***" if colon else creds
    return f"{scheme}://{auth}@{host}"


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err["loc"]) or "<root>"
        parts.append(f"{loc}：{err['msg']}")
    return "；".join(parts)


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _flat_override_data(overrides: _FlatOverrides) -> dict[str, Any]:
    """把显式给出的覆盖项折成嵌套 dict；空字符串表示「没给」，不覆盖。"""
    data: dict[str, Any] = {}
    for field_name, path in FLAT_OVERRIDE_TARGETS:
        if field_name not in overrides.model_fields_set:
            continue
        value = getattr(overrides, field_name)
        if value in (None, ""):
            continue
        node = data
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return data


def _resolve_config_path(path: str | None, environ: Mapping[str, str]) -> tuple[str, bool]:
    """返回 (配置文件绝对路径, 是否由调用方显式指定)。

    `XHS_CONFIG_PATH` 只从进程环境变量读——`.env` 的位置又取决于配置文件位置，
    从 `.env` 里读会形成循环依赖。
    """
    if path:
        return os.path.abspath(path), True
    from_env = str(environ.get("XHS_CONFIG_PATH") or "").strip()
    if from_env:
        return os.path.abspath(from_env), True
    return os.path.abspath(DEFAULT_CONFIG_PATH), False


def _read_toml(config_path: str, explicit: bool) -> dict[str, Any]:
    if not os.path.exists(config_path):
        if explicit:
            raise ConfigError(
                f"配置文件不存在：{config_path}",
                "检查 XHS_CONFIG_PATH / load_config(path=...) 是否写对，"
                "或改用默认的 config/config.toml")
        return {}
    try:
        with open(config_path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"config.toml 解析失败：{exc}",
            "检查 config/config.toml 的语法（常见原因：缺引号、表头少方括号）") from exc
    except OSError as exc:
        raise ConfigError(f"config.toml 读取失败：{exc}", f"确认文件可读：{config_path}") from exc


def load_config(path: str | None = None, *, env_file: str | None = None) -> AppConfig:
    """读取配置。优先级：环境变量 > `.env` > config.toml > 代码内默认值。

    - 默认配置文件缺失时退到代码内默认值（开箱可跑）；显式指定的路径缺失则报错。
    - 不再把 `.env` 注入 `os.environ`，密钥只通过 `EnvView` 按需解析。
    """
    environ = dict(os.environ)
    config_path, explicit = _resolve_config_path(path, environ)
    if env_file is None:
        env_file = os.path.join(os.path.dirname(config_path), ".env")
    has_env_file = bool(env_file) and os.path.exists(env_file)

    env_view = EnvView(dotenv_values(env_file) if has_env_file else {}, environ)
    try:
        overrides = _FlatOverrides(_env_file=env_file if has_env_file else None)
    except ValidationError as exc:
        raise ConfigError(f"环境变量取值不合法：{_format_validation_error(exc)}",
                          "按 docs/contracts/配置契约.md 第二节修正对应的环境变量") from exc

    data = _deep_merge(_read_toml(config_path, explicit), _flat_override_data(overrides))
    try:
        cfg = AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            f"配置不合法：{_format_validation_error(exc)}",
            "按 docs/contracts/配置契约.md 第三节修正字段名与取值范围"
            "（环境变量覆盖项同样受这些规则约束）") from exc

    cfg._config_path = config_path
    cfg._env = env_view
    for section in (cfg.llm, cfg.vision, cfg.embedding):
        section._env = env_view
    return cfg


def config_to_dict(cfg: AppConfig) -> dict[str, Any]:
    """配置快照（脱敏）：不含任何密钥，可安全写入日志或报告。"""
    data = cfg.model_dump()
    data["config_path"] = cfg.config_path
    data["langfuse_public_key"] = "***" if cfg.langfuse_keys[0] else ""
    data["langfuse_secret_key"] = "***" if cfg.langfuse_keys[1] else ""
    data["langfuse_host"] = cfg.langfuse_keys[2]
    return data
