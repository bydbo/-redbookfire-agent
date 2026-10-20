"""E8 历史快照回填管理入口：plan 默认只读；apply 需已审查计划；verify 核验批次。

不调用模型、不移动文件、不打印密钥。正式库 apply 必须在停写窗口获得单独授权。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

from xhs_agent.config import PROJECT_ROOT, load_config
from xhs_agent.db.session import create_engine_from_config, create_session_factory
from xhs_agent.services.e8_snapshot_migration import (
    apply_snapshot,
    plan_snapshot,
    verify_snapshot,
)

_VERSION_PATHS = ("src", "alembic", "scripts")


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", check=True)
    return result.stdout


def implementation_version() -> str:
    head = _git("rev-parse", "HEAD").strip()
    digest = hashlib.sha256()
    for args in (("diff", "--binary", "HEAD", "--", *_VERSION_PATHS),
                 ("status", "--porcelain", "--", *_VERSION_PATHS)):
        digest.update(_git(*args).encode("utf-8"))
        digest.update(b"\0")
    untracked = _git("ls-files", "--others", "--exclude-standard", "--", *_VERSION_PATHS)
    for relative in sorted(item for item in untracked.splitlines() if item):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((Path(PROJECT_ROOT) / relative).read_bytes())
        digest.update(b"\0")
    return f"{head}+worktree:{digest.hexdigest()[:16]}"


async def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    engine = create_engine_from_config(cfg)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            if args.command == "plan":
                plan = await plan_snapshot(session, code_version=implementation_version())
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
                print(f"plan={output} batch={plan['batch_id']} "
                      f"counts={json.dumps(plan['counts'], ensure_ascii=False)} "
                      f"executable={plan['executable']}")
                return 0 if plan["executable"] else 2
            if args.command == "apply":
                plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
                result = await apply_snapshot(
                    session, plan, code_version=implementation_version())
            else:
                result = await verify_snapshot(session, uuid.UUID(args.batch))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E8 历史快照回填 plan/apply/verify")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="只读生成回填计划")
    plan.add_argument("--output", required=True)
    apply = sub.add_parser("apply", help="应用已审查计划")
    apply.add_argument("--plan", required=True)
    verify = sub.add_parser("verify", help="核验批次")
    verify.add_argument("--batch", required=True)
    try:
        return asyncio.run(run(parser.parse_args(argv)))
    except (ValueError, OSError) as exc:
        print(f"E8 快照迁移未完成：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
