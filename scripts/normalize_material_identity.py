"""E6 管理入口：plan 默认只读，apply 要求指定已审查计划，verify 核验批次。

运行产物含本地路径，默认保存 runs/_material_identity/；不打印密钥、不调用模型。
必须在停写窗口对真实库 apply，完整操作说明见 docs/E6素材身份归一化执行与验收.md。
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
from xhs_agent.services.material_identity_migration import (
    apply_migration,
    plan_migration,
    verify_migration,
)
from xhs_agent.tools.material_paths import resolve_material

#: 参与实现版本指纹的目录：业务代码、迁移、管理脚本
_VERSION_PATHS = ("src", "alembic", "scripts")


def _git(*args: str) -> str:
    """执行只读 git 命令；失败明确报错（不吞 stderr、不打印凭据）。

    Windows 控制台 locale 常是 GBK，git 输出含中文路径时按 UTF-8 解码并替换非法字节，
    不能依赖默认编码（否则 code_version 计算本身会崩）。
    """
    result = subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return result.stdout


def implementation_version() -> str:
    """绑定"被审查的实现"，而不只是 Git HEAD。

    正式迁移应使用已提交版本；在未提交工作区生成计划时，把源码 diff、未跟踪源码内容与
    状态一并纳入指纹。apply 用**当前**指纹重建计划，任何实现改动都会让旧计划失效。
    """
    head = _git("rev-parse", "HEAD").strip()
    digest = hashlib.sha256()
    for args in (
        ("diff", "--binary", "HEAD", "--", *_VERSION_PATHS),
        ("status", "--porcelain", "--", *_VERSION_PATHS),
    ):
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
                manifest = json.loads(Path(args.roots).read_text(encoding="utf-8"))
                version = implementation_version()
                plan = await plan_migration(session, manifest["roots"],
                                            relative_basis=manifest.get("relative_basis", "material"),
                                            code_version=version)
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                missing: list[str] = []
                for group in plan["groups"]:
                    try:
                        if not Path(resolve_material(cfg.materials_dir(), group["path"])).is_file():
                            missing.append(group["path"])
                    except (ValueError, OSError):
                        missing.append(f"{group['path']}（不可安全访问）")
                lines = ["# 素材身份迁移预检", "", f"批次：{plan['batch_id']}",
                         f"规则：{plan['rule_version']}；代码：{version}",
                         f"数据库：{plan['database']['name']}（oid {plan['database']['oid']}；"
                         f"集群标识来源 {plan['database']['cluster_source']}）",
                         f"schema：{plan['schema_revision']}",
                         f"可执行：{'是' if plan.get('executable') else '否（无法读取集群身份）'}",
                         "", "仅盘点，未修改数据库。文件可用性不参与保留 UUID 选择。", "",
                         f"计数：{json.dumps(plan['counts'], ensure_ascii=False)}",
                         f"归档条目：{len(plan['archive_manifest'])}；"
                         f"旧 JSON 引用行：{len(plan['references'])}；"
                         f"关键帧素材：{len(plan['keyframe_references'])}；"
                         f"关联运行：{len(plan['affected_runs'])}",
                         f"未解决问题：{len(plan['issues'])}",
                         f"本机缺失文件：{len(missing)}", ""]
                if not plan.get("executable"):
                    lines.append("> 警告：当前账号无权读取 pg_control_system()，本计划**不可执行**。"
                                 "只读预检可继续；正式 apply 必须由具备 pg_monitor 或同等权限的"
                                 "操作者重新生成计划。")
                    lines.append("")
                if plan["database"]["cluster_source"] != "pg_control_system":
                    lines.append("> 警告：集群身份未读取，不能用于判断两个集群是否相同。")
                    lines.append("")
                for issue in plan["issues"]:
                    lines.append(f"- 问题 {issue['id']}：{issue['reason']}")
                for path in missing:
                    lines.append(f"- 文件缺失（不删除记录）：{path}")
                for group in plan["groups"]:
                    lines.append(f"- {group['path']}：{len(group['members'])} 行；"
                                 f"保留 {group['canonical_id']}；"
                                 f"差异字段 {', '.join(group['different_fields'])}")
                for reference in plan["references"][:50]:
                    lines.append(f"- 旧引用 {reference['table']}/{reference['key']}："
                                 f"{', '.join(reference['material_ids'])}")
                output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
                print(f"plan={output} issues={len(plan['issues'])} batch={plan['batch_id']}")
                return 2 if plan["issues"] else 0
            if args.command == "apply":
                plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
                result = await apply_migration(session, plan,
                                               code_version=implementation_version())
            else:
                result = await verify_migration(session, uuid.UUID(args.batch), exact=not args.audit_only)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="只读预检，生成 JSON 与 Markdown")
    plan.add_argument("--roots", required=True, help="显式历史根映射 JSON")
    plan.add_argument("--output", default=str(Path(PROJECT_ROOT, "runs/_material_identity/plan.json")))
    apply = sub.add_parser("apply", help="仅在停写并备份后应用已审查计划")
    apply.add_argument("--plan", required=True)
    verify = sub.add_parser("verify", help="核验停写窗口内精确结果")
    verify.add_argument("--batch", required=True)
    verify.add_argument("--audit-only", action="store_true",
                        help="恢复业务后只核验归档 / 别名 / 路径，不声明切换数据完全一致")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except (ValueError, OSError) as exc:
        print(f"身份迁移未完成：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
