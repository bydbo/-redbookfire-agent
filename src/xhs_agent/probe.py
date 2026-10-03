"""启动前置检查（preflight）：《配置契约》第四节的 7 步。

按顺序执行，**任一步失败即中止启动**：

| 步骤 | 检查 | 失败退出码 |
| --- | --- | --- |
| 1 | 必填环境变量齐全且格式合法（按 §2.1.1 分级生效） | 2 |
| 2 | `config.toml` 可解析、字段类型与取值合法 | 2 |
| 3 | 数据库可连接 | 3 |
| 4 | 扩展 `vector` / `pg_trgm` 已装且 pgvector ≥ 0.5.0 | 3 |
| 5 | 数据库迁移版本处于 Alembic head | 3 |
| 6 | Redis 可连接（`PING`） | 3 |
| 7 | `[frontend].serve = true` 时 `frontend/dist` 存在且非空 | 2 |

**模型服务不在启动时探测**：启动即调用模型会产生费用，也容易被网络抖动误判；模型可用性由
`GET /api/health` 按需探测，不计入启动成败。

用法：

    uv run python -m xhs_agent.probe      # 退出码 0 = 可以启动，2 = 配置错误，3 = 依赖不可用
    uv run python -m xhs_agent.serve      # 推荐启动入口：先查这 7 步，再交给 uvicorn

退出码边界：`python -m xhs_agent.probe` 与 `python -m xhs_agent.serve` 给精确的 2/3；
`uvicorn xhs_agent.api.main:app` 这条路径只保证"检查不过就不启动"——uvicorn 自己在 lifespan
启动失败时**固定退出 3**（实测），精确码请走上面两个入口。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from .api.frontend import FIX_HINT as FRONTEND_FIX_HINT
from .api.frontend import dist_dir_if_available
from .config import (
    PROJECT_ROOT,
    AppConfig,
    ConfigError,
    EnvView,
    _mask_dsn,
    load_config,
)
from .db.session import database_url

# 当前阶段：S2.1 起进入 P2（DATABASE_URL 必填）；S3.4b 落地 Celery 队列后进入 P3
# （REDIS_URL 变为必填）。
CURRENT_STAGE = "P3"

E_CONFIG_MISSING = "E_CONFIG_MISSING"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_DB_UNAVAILABLE = "E_DB_UNAVAILABLE"
E_DB_EXTENSION = "E_DB_EXTENSION"
E_DB_MIGRATION = "E_DB_MIGRATION"
E_REDIS_UNAVAILABLE = "E_REDIS_UNAVAILABLE"
E_FRONTEND_DIST_MISSING = "E_FRONTEND_DIST_MISSING"

_STAGE_RANK = {"P1": 1, "P2": 2, "P3": 3}

# 依赖型必填变量：到了对应阶段才要求，并要求协议前缀正确。
_STAGED_VARIABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("DATABASE_URL", "P2", ("postgresql+asyncpg://",)),
    ("REDIS_URL", "P3", ("redis://", "rediss://")),
)

_LANGFUSE_NAMES = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")

# 契约 §四 第 4 步：这两个扩展必须就位，且 pgvector 版本不低于这个线
REQUIRED_EXTENSIONS = ("vector", "pg_trgm")
PGVECTOR_MIN_VERSION = (0, 5, 0)

ALEMBIC_INI = str(Path(PROJECT_ROOT) / "alembic.ini")

REDIS_PROBE_TIMEOUT_S = 2.0

USAGE = """用法：uv run python -m xhs_agent.probe

打印脱敏配置摘要，并执行启动前置检查（《配置契约》第四节第 1–7 步）。
退出码：0 = 通过，可以启动；2 = 配置错误；3 = 依赖不可用。
"""


@dataclass
class Problem:
    """一条前置检查问题：错误码 + 变量/字段名 + 说明 + 可照做的修复提示。"""

    code: str
    name: str
    message: str
    fix: str
    step: int = 0
    kind: str = "config"          # config → 退出码 2；dependency → 退出码 3

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
        """退出码由**首个问题的 kind** 推出（检查按契约顺序跑，首个失败即停）。"""
        if not self.problems:
            return 0
        return 3 if self.problems[0].kind == "dependency" else 2


class PreflightFailed(RuntimeError):
    """启动前置检查未通过：调用方打印问题清单后中止启动。"""

    def __init__(self, report: PreflightReport) -> None:
        super().__init__(f"启动前置检查未通过：{len(report.problems)} 项问题")
        self.report = report


def _key_fix(name: str, cfg: AppConfig) -> str:
    env_file = os.path.join(os.path.dirname(cfg.config_path), ".env")
    return (f"把 {name}=<你的密钥> 写进 {env_file}（模板见 config/.env.example），"
            f"或直接导出同名环境变量")


def _already_reported(report: PreflightReport, code: str, name: str) -> bool:
    return any(item.code == code and item.name == name for item in report.problems)


def preflight(cfg: AppConfig, env: EnvView | None = None,
              stage: str = CURRENT_STAGE) -> PreflightReport:
    """第 1–2 步：按契约 §2.1.1 分级校验必填项；只校验当前阶段要求生效的变量。"""
    env = env if env is not None else cfg.env_view
    rank = _STAGE_RANK.get(stage, _STAGE_RANK[CURRENT_STAGE])
    report = PreflightReport(stage=stage)

    # 1) 文本模型密钥：P1 起必填（provider 已无降级分支）
    if not (env.get("XHS_LLM_API_KEY") or env.get(cfg.llm.api_key_env)):
        report.problems.append(Problem(
            E_CONFIG_MISSING, cfg.llm.api_key_env,
            f"缺少文本模型密钥：{cfg.llm.api_key_env} 为空（变量名由 [llm].api_key_env 指定）",
            _key_fix(cfg.llm.api_key_env, cfg), step=1))

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
                _key_fix(key_env, cfg), step=1))

    # 3) 依赖型变量：到阶段才要求；要求生效时同时校验协议前缀
    for name, required_stage, schemes in _STAGED_VARIABLES:
        if rank < _STAGE_RANK[required_stage]:
            continue
        value = env.get(name)
        if not value:
            report.problems.append(Problem(
                E_CONFIG_MISSING, name,
                f"缺少 {name}（{required_stage} 起必填）",
                f"把 {name}=<连接串> 写进 config/.env 或导出同名环境变量", step=1))
        elif not value.startswith(schemes):
            report.problems.append(Problem(
                E_CONFIG_INVALID, name,
                f"{name} 必须以前缀 {' 或 '.join(schemes)} 开头",
                f"按《配置契约》§2.1 修正 {name} 的连接串前缀", step=1))

    # 3.5) 多模态打标开着但机器没有 ffmpeg：属于能力裁剪，只告警、不阻断启动
    if cfg.vision.enabled and cfg.vision.resolved_key() and not cfg.vision.available():
        report.warnings.append(
            "多模态打标不会生效：机器上没有 ffmpeg / ffprobe（[vision].enabled = true，属能力裁剪）")

    # 4) 可观测三件套：不齐只告警，不阻断启动
    values = {name: env.get(name) for name in _LANGFUSE_NAMES}
    if any(values.values()) and not all(values.values()):
        missing = "、".join(name for name in _LANGFUSE_NAMES if not values[name])
        report.warnings.append(f"调用追踪已关闭：{missing} 未配置，LANGFUSE_* 需要三者齐全")

    return report


# ---------------------------------------------------------------------------
# 第 3–7 步：依赖与产物检查（每个函数返回 Problem | None，方便单独测）
# ---------------------------------------------------------------------------


def _version_tuple(value: str) -> tuple[int, ...]:
    """把 "0.8.6" 这类版本串变成可比较的元组（非数字段当 0，避免脏数据把检查搞崩）。"""
    parts: list[int] = []
    for chunk in str(value or "").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


async def check_database(cfg: AppConfig, engine: AsyncEngine) -> Problem | None:
    """第 3 步：数据库可连接（`SELECT 1`）。失败时打印脱敏 DSN 与底层错误。"""
    dsn = _mask_dsn(database_url(cfg))
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:      # 连接/认证/超时都算依赖不可用
        return Problem(
            E_DB_UNAVAILABLE, "DATABASE_URL",
            f"数据库连不上：{dsn}（{type(exc).__name__}: {exc}）",
            "先起本地依赖：docker compose up -d --wait；再核对 config/.env 的 DATABASE_URL",
            step=3, kind="dependency")
    return None


async def check_extensions(engine: AsyncEngine) -> Problem | None:
    """第 4 步：`vector` 与 `pg_trgm` 已安装，且 pgvector ≥ 0.5.0。"""
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT extname, extversion FROM pg_extension "
            "WHERE extname IN ('vector', 'pg_trgm')"))
        installed = {row.extname: row.extversion for row in result.all()}

    missing = [name for name in REQUIRED_EXTENSIONS if name not in installed]
    if missing:
        return Problem(
            E_DB_EXTENSION, "pg_extension",
            f"数据库缺少扩展：{'、'.join(missing)}（已装：{installed or '无'}）",
            "本地用 docker compose up -d --wait 起带扩展的镜像（pgvector/pgvector:0.8.6-pg16）；"
            "扩展由 docker/postgres/init/01-extensions.sql 预建，迁移里只用 IF NOT EXISTS 兜底",
            step=4, kind="dependency")

    version = str(installed.get("vector", ""))
    if _version_tuple(version) < PGVECTOR_MIN_VERSION:
        want = ".".join(str(part) for part in PGVECTOR_MIN_VERSION)
        return Problem(
            E_DB_EXTENSION, "pgvector",
            f"pgvector 版本过低：{version} < {want}",
            "升级到 pgvector/pgvector:0.8.6-pg16（docker-compose.yml 里固定 tag，升级后重建容器）",
            step=4, kind="dependency")
    return None


def expected_migration_head() -> str:
    """从 `alembic/` 读当前 head（不连库）。"""
    config = AlembicConfig(ALEMBIC_INI)
    return str(ScriptDirectory.from_config(config).get_current_head() or "")


async def check_migration(engine: AsyncEngine) -> Problem | None:
    """第 5 步：数据库迁移版本处于 head。表不存在 = 从未迁移。"""
    expected = expected_migration_head()
    fix = "跑迁移：uv run alembic upgrade head（回滚：uv run alembic downgrade base）"
    async with engine.connect() as conn:
        exists = await conn.scalar(text("SELECT to_regclass('public.alembic_version') IS NOT NULL"))
        current = ""
        if exists:
            current = str(await conn.scalar(text("SELECT version_num FROM alembic_version")) or "")
    if not current:
        return Problem(
            E_DB_MIGRATION, "alembic_version",
            f"数据库还没做过迁移（期望 head：{expected or '未知'}）",
            fix, step=5, kind="dependency")
    if expected and current != expected:
        return Problem(
            E_DB_MIGRATION, "alembic_version",
            f"数据库迁移版本落后/不一致：当前 {current}、期望 {expected}",
            fix, step=5, kind="dependency")
    return None


async def check_redis(cfg: AppConfig) -> Problem | None:
    """第 6 步：Redis 可连接（`PING`）。"""
    url = (cfg.env_view.get("REDIS_URL") or "").strip()
    client = aioredis.from_url(url, socket_connect_timeout=REDIS_PROBE_TIMEOUT_S,
                               socket_timeout=REDIS_PROBE_TIMEOUT_S)
    try:
        await client.ping()
    except Exception as exc:
        return Problem(
            E_REDIS_UNAVAILABLE, "REDIS_URL",
            f"Redis 连不上：{_mask_dsn(url)}（{type(exc).__name__}: {exc}）",
            "先起本地依赖：docker compose up -d --wait；再核对 config/.env 的 REDIS_URL"
            "（建议写 127.0.0.1 而不是 localhost）",
            step=6, kind="dependency")
    finally:
        await client.aclose()
    return None


def check_frontend(cfg: AppConfig) -> Problem | None:
    """第 7 步：`serve = true` 时前端构建产物存在且非空。"""
    if not cfg.frontend.serve:
        return None
    if dist_dir_if_available(cfg) is not None:
        return None
    return Problem(
        E_FRONTEND_DIST_MISSING, "frontend.dist_dir",
        f"[frontend].serve = true 但构建产物不可用：{cfg.frontend_dist_dir()}",
        FRONTEND_FIX_HINT, step=7, kind="config")


async def run_startup_checks(cfg: AppConfig) -> PreflightReport:
    """按契约 §四 顺序跑 7 步；**任一失败即停**（第 1–2 步的配置问题一次列全）。"""
    report = preflight(cfg)
    if not report.ok:
        return report

    engine = create_async_engine(database_url(cfg), poolclass=NullPool)
    try:
        problem = await check_database(cfg, engine)
        if problem is not None:
            report.problems.append(problem)
            return report
        for check in (check_extensions, check_migration):
            problem = await check(engine)
            if problem is not None:
                report.problems.append(problem)
                return report
    finally:
        await engine.dispose()

    problem = await check_redis(cfg)
    if problem is not None:
        report.problems.append(problem)
        return report
    problem = check_frontend(cfg)
    if problem is not None:
        report.problems.append(problem)
    return report


def report_lines(report: PreflightReport, *, include_warnings: bool = True) -> list[str]:
    """把报告折成可打印的行（CLI、serve 包装与 lifespan 共用一套文案）。"""
    lines: list[str] = []
    if include_warnings:
        lines.extend(f"WARN: {warning}" for warning in report.warnings)
    for problem in report.problems:
        lines.extend(problem.lines())
    return lines


def main(argv: list[str] | None = None) -> int:
    """命令行入口：打印脱敏摘要 + 7 步检查结果，返回精确退出码（0/2/3）。"""
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

    report = asyncio.run(run_startup_checks(cfg))
    for line in report_lines(report):
        print(line)
    if report.ok:
        print(f"前置检查通过（阶段 {report.stage}）：配置与依赖都可以启动")
        return 0
    print(f"前置检查失败（阶段 {report.stage}）：{len(report.problems)} 项问题，"
          f"退出码 {report.exit_code}")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
