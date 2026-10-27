"""E8 历史快照回填管理入口：plan 默认只读；apply 需已审查计划；verify 核验批次。

不调用模型、不移动文件、不打印密钥。正式库 apply 必须在停写窗口获得单独授权。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from pathlib import Path

from xhs_agent.config import PROJECT_ROOT, load_config
from xhs_agent.db.session import create_engine_from_config, create_session_factory
from xhs_agent.services.e8_snapshot_migration import (
    apply_snapshot,
    plan_snapshot,
    verify_snapshot,
)

_VERSION_PATHS = ("src", "alembic", "scripts", "config/config.toml",
                  "alembic.ini", "pyproject.toml", "uv.lock")


def implementation_version(root: Path | None = None) -> str:
    """Content identity shared by checkout and image; requires neither Git nor env overrides.

    Hash source/runtime assets, migrations and locked dependencies. Normalize only
    text line endings so Windows checkout and Linux COPY produce the same identity.
    Bytecode / tool caches are excluded because importing code generates them.
    """
    base = root or Path(PROJECT_ROOT)
    files: list[Path] = []
    for relative in _VERSION_PATHS:
        target = base / relative
        if not target.exists():
            raise ValueError(f"缺少实现版本所需文件：{relative}")
        if target.is_dir():
            files.extend(path for path in target.rglob("*") if path.is_file())
        else:
            files.append(target)
    fingerprint = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(base).as_posix()):
        if (any(part in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
                for part in path.parts) or path.suffix in {".pyc", ".pyo"}):
            continue
        relative = path.relative_to(base).as_posix()
        payload = path.read_bytes().replace(b"\r\n", b"\n")
        fingerprint.update(relative.encode("utf-8") + b"\0")
        fingerprint.update(hashlib.sha256(payload).digest())
    return f"source-sha256:{fingerprint.hexdigest()}"


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
