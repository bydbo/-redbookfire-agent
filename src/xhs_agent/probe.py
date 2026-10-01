"""启动前置检查（preflight）。

实现《配置契约》第四节的前两步：

1. 必填环境变量齐全且格式合法（按 §2.1.1 分级生效）；
2. `config.toml` 可解析、字段类型与取值在允许范围内（由 `config.py` 的模型校验完成）。

第 3–7 步（数据库连接、扩展版本、Alembic head、Redis PING、前端 dist）依赖尚未落地的
组件，属于 backlog 的 S3.8。**模型服务不在启动时探测**（会产生费用，也容易被网络抖动误判）。

用法：

    uv run python -m xhs_agent.probe      # 退出码 0 = 可以启动，2 = 配置错误
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

from .config import AppConfig, ConfigError, EnvView, load_config

# 当前阶段：S2.1 起进入 P2（DATABASE_URL 变为必填）；E3 落地队列后改成 "P3"。
CURRENT_STAGE = "P2"

E_CONFIG_MISSING = "E_CONFIG_MISSING"
E_CONFIG_INVALID = "E_CONFIG_INVALID"

_STAGE_RANK = {"P1": 1, "P2": 2, "P3": 3}

# 依赖型必填变量：到了对应阶段才要求，并要求协议前缀正确。
_STAGED_VARIABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("DATABASE_URL", "P2", ("postgresql+asyncpg://",)),
    ("REDIS_URL", "P3", ("redis://", "rediss://")),
)

_LANGFUSE_NAMES = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")

USAGE = """用法：uv run python -m xhs_agent.probe

打印脱敏配置摘要，并执行启动前置检查（《配置契约》第四节第 1–2 步）。
退出码：0 = 通过，可以启动；2 = 配置错误。
"""


@dataclass
class Problem:
    """一条配置问题：错误码 + 变量/字段名 + 说明 + 可照做的修复提示。"""

    code: str
    name: str
    message: str
    fix: str

    def lines(self) -> list[str]:
        return [f"{self.code}: {self.name}", f"  {self.message}", f"  修复：{self.fix}"]


@dataclass
class PreflightReport:
    """前置检查结果：问题清单 + 警告清单。"""

    stage: str = CURRENT_STAGE
    problems: list[Problem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 2


def _key_fix(name: str, cfg: AppConfig) -> str:
    env_file = os.path.join(os.path.dirname(cfg.config_path), ".env")
    return (f"把 {name}=<你的密钥> 写进 {env_file}（模板见 config/.env.example），"
            f"或直接导出同名环境变量")


def _already_reported(report: PreflightReport, code: str, name: str) -> bool:
    return any(item.code == code and item.name == name for item in report.problems)


def preflight(cfg: AppConfig, env: EnvView | None = None,
              stage: str = CURRENT_STAGE) -> PreflightReport:
    """按契约 §2.1.1 分级校验必填项；只校验当前阶段要求生效的变量。"""
    env = env if env is not None else cfg.env_view
    rank = _STAGE_RANK.get(stage, _STAGE_RANK[CURRENT_STAGE])
    report = PreflightReport(stage=stage)

    # 1) 文本模型密钥：P1 起必填（provider 已无降级分支）
    if not (env.get("XHS_LLM_API_KEY") or env.get(cfg.llm.api_key_env)):
        report.problems.append(Problem(
            E_CONFIG_MISSING, cfg.llm.api_key_env,
            f"缺少文本模型密钥：{cfg.llm.api_key_env} 为空（变量名由 [llm].api_key_env 指定）",
            _key_fix(cfg.llm.api_key_env, cfg)))

    # 2) 辅助服务密钥：仅当多模态打标或向量召回启用时必填
    for label, enabled, key_env in (
        ("多模态打标（[vision].enabled = true）", cfg.vision.enabled, cfg.vision.api_key_env),
        ("向量召回（[embedding].enabled = true）", cfg.embedding.enabled, cfg.embedding.api_key_env),
    ):
        if enabled and not env.get(key_env) and not _already_reported(
                report, E_CONFIG_MISSING, key_env):
            report.problems.append(Problem(
                E_CONFIG_MISSING, key_env,
                f"{label} 已启用，但密钥 {key_env} 为空",
                _key_fix(key_env, cfg)))

    # 3) 依赖型变量：到阶段才要求；要求生效时同时校验协议前缀
    for name, required_stage, schemes in _STAGED_VARIABLES:
        if rank < _STAGE_RANK[required_stage]:
            continue
        value = env.get(name)
        if not value:
            report.problems.append(Problem(
                E_CONFIG_MISSING, name,
                f"缺少 {name}（{required_stage} 起必填）",
                f"把 {name}=<连接串> 写进 config/.env 或导出同名环境变量"))
        elif not value.startswith(schemes):
            report.problems.append(Problem(
                E_CONFIG_INVALID, name,
                f"{name} 必须以前缀 {' 或 '.join(schemes)} 开头",
                f"按《配置契约》§2.1 修正 {name} 的连接串前缀"))

    # 4) 可观测三件套：不齐只告警，不阻断启动
    values = {name: env.get(name) for name in _LANGFUSE_NAMES}
    if any(values.values()) and not all(values.values()):
        missing = "、".join(name for name in _LANGFUSE_NAMES if not values[name])
        report.warnings.append(f"调用追踪已关闭：{missing} 未配置，LANGFUSE_* 需要三者齐全")

    return report


def main(argv: list[str] | None = None) -> int:
    """命令行入口：打印脱敏摘要 + 前置检查结果，返回退出码。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        if args[0] in {"-h", "--help"}:
            print(USAGE)
            return 0
        print(f"未知参数：{' '.join(args)}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"{exc.code}: {exc.message}")
        if exc.fix:
            print(f"  修复：{exc.fix}")
        return 2

    print("配置摘要（已脱敏）：")
    print(json.dumps(cfg.describe(), ensure_ascii=False, indent=2))

    report = preflight(cfg)
    for warning in report.warnings:
        print(f"WARN: {warning}")
    for problem in report.problems:
        print("\n".join(problem.lines()))
    if report.ok:
        print(f"前置检查通过（阶段 {report.stage}）：配置可以启动")
        return 0
    print(f"前置检查失败（阶段 {report.stage}）：{len(report.problems)} 项配置问题，退出码 2")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
