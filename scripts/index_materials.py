"""素材索引入库（运维命令）：首次全量建库 / 手动重建。

用途：不依赖 HTTP 接口的一次性建库入口。首次使用素材库时跑一次全量，之后每次分析前
      会自动做增量同步（见 `xhs_agent.services.materials.sync_materials`）。
输入：无参数即可（读 `AppConfig`）；`--max-vision-items N` 控制本次视觉打标的条数上限。
输出：打印同步报告（扫描 / 新增 / 更新 / 未变 / 删除）；退出码 0 = 成功、1 = 配置或依赖问题。

用法：

    uv run python scripts/index_materials.py
    uv run python scripts/index_materials.py --max-vision-items 20
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
from xhs_agent.services.materials import sync_materials
from xhs_agent.tools.vision import make_describer


async def _run(cfg: AppConfig, max_vision_items: int) -> int:
    engine = create_engine_from_config(cfg)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            report = await sync_materials(session, cfg, vision=make_describer(cfg),
                                          max_vision_items=max_vision_items)
        print(report.summary())
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把素材目录增量同步进 materials 表")
    parser.add_argument("--max-vision-items", type=int, default=50,
                        help="本次视觉打标的素材条数上限（默认 50）")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
        database_url(cfg)  # 缺 DATABASE_URL 时提前报错（不降级）
    except ConfigError as exc:
        print(f"E_CONFIG_INVALID: {exc.message}", file=sys.stderr)
        if exc.fix:
            print(f"修复：{exc.fix}", file=sys.stderr)
        return 1

    try:
        return asyncio.run(_run(cfg, args.max_vision_items))
    except ConfigError as exc:
        print(f"E_CONFIG_INVALID: {exc.message}", file=sys.stderr)
        if exc.fix:
            print(f"修复：{exc.fix}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
