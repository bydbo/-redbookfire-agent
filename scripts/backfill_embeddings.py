"""向量回填（运维命令）：给缺向量或换了模型的素材补齐 `embedding`。

用途：`materials.embedding` 的唯一写入入口。首次建库、素材内容变更（同步会清空向量）
      与更换 embedding 模型之后各跑一次；之后由 S2.6 的分析前置步骤按需调用。
输入：无参数即可（读 `AppConfig`）；`--batch-size N` 覆盖每批条数，`--limit N` 限制本次条数。
输出：打印回填报告（待处理 / 写入 / 跳过空文本 / 失败 / 批次 / prompt_tokens）；
      退出码 0 = 成功（含待处理为 0）、1 = 配置、依赖或上游失败。

用法：

    uv run python scripts/backfill_embeddings.py
    uv run python scripts/backfill_embeddings.py --batch-size 10 --limit 200
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from xhs_agent.config import AppConfig, ConfigError, load_config
from xhs_agent.db.session import (
    create_engine_from_config,
    create_session_factory,
    database_url,
)
from xhs_agent.services.materials import backfill_embeddings
from xhs_agent.tools.embedding import EmbeddingError


async def _run(cfg: AppConfig, batch_size: int | None, limit: int | None) -> int:
    engine = create_engine_from_config(cfg)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            # embedder 由服务层按配置自造（S3.5：httpx 异步客户端，连接池一次调用一个）
            report = await backfill_embeddings(session, cfg, batch_size=batch_size, limit=limit)
        print(report.summary())
        return 0 if report.failed == 0 else 1
    finally:
        await engine.dispose()


def _report_failure(exc: Exception) -> int:
    code = getattr(exc, "code", "E_EMBEDDING")
    message = getattr(exc, "message", None) or str(exc)
    print(f"{code}: {message}", file=sys.stderr)
    fix = getattr(exc, "fix", "")
    if fix:
        print(f"修复：{fix}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给缺向量或换了模型的素材回填 embedding")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="每批提交的条数（默认取 [embedding].batch_size）")
    parser.add_argument("--limit", type=int, default=None,
                        help="本次最多处理多少条（默认全部）")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
        database_url(cfg)          # 缺 DATABASE_URL 提前报错（不降级）
        if not cfg.embedding.enabled:
            raise ConfigError(
                "向量召回未启用（[embedding].enabled = false）",
                "把 config/config.toml 的 [embedding].enabled 设为 true 后再回填")
        if not cfg.embedding.resolved_key():   # 缺密钥提前报错（不降级）
            raise ConfigError(
                f"[embedding].enabled = true 但密钥 {cfg.embedding.api_key_env} 为空",
                "把密钥写进 config/.env（模板见 config/.env.example），或导出同名环境变量")
    except (ConfigError, EmbeddingError) as exc:
        return _report_failure(exc)

    try:
        return asyncio.run(_run(cfg, args.batch_size, args.limit))
    except (ConfigError, EmbeddingError) as exc:
        return _report_failure(exc)


if __name__ == "__main__":
    raise SystemExit(main())
