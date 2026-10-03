"""推荐启动入口：先跑 7 步启动前置检查，再交给 uvicorn（S3.8）。

用法：`uv run python -m xhs_agent.serve [--host 127.0.0.1] [--port 8000]`

为什么需要这层包装：uvicorn 在 lifespan 启动失败时**固定退出 3**（实测确认），而《配置契约》
§四 要求配置错误退 2、依赖不可用退 3。这层薄包装先自己查一遍并返回精确退出码，通过后再起
uvicorn（用 `create_app(cfg, check_startup=False)`，不让 lifespan 再查第二遍）。
`uvicorn xhs_agent.api.main:app` 仍然可用，只是那条路径失败恒退 3。

也顺带成为 S4.4 容器镜像的天然 CMD 候选。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from .api.main import create_app
from .config import ConfigError, load_config
from .probe import report_lines, run_startup_checks


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m xhs_agent.serve",
                                     description="先跑启动前置检查（退出码 2/3 对齐契约），再起 uvicorn")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """返回 0 = 正常退出；2 = 配置错误；3 = 依赖不可用（都在绑定端口之前失败）。"""
    args = parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        if exc.fix:
            print(f"  修复：{exc.fix}", file=sys.stderr)
        return 2

    report = asyncio.run(run_startup_checks(cfg))
    for line in report_lines(report):
        print(line, file=sys.stderr)
    if not report.ok:
        print(f"启动前置检查未通过（阶段 {report.stage}）：{len(report.problems)} 项问题，"
              f"退出码 {report.exit_code}", file=sys.stderr)
        return report.exit_code

    print(f"前置检查通过（阶段 {report.stage}）：启动 uvicorn http://{args.host}:{args.port}",
          file=sys.stderr)
    uvicorn.run(create_app(cfg, check_startup=False), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
