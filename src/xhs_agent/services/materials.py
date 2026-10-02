"""素材索引同步：把素材目录的变化增量落进 `materials` 表。

用途：一次增量同步——扫描目录 → 规划新增/更新/未变/删除 → 对变化的文件重建素材行并落库。
输入：`AsyncSession`、`AppConfig`；可选注入 `vision`（`(frames, hint) -> dict | None` 的
      视觉打标可调用对象）与 `max_vision_items`（本次最多打标多少条，控制成本）。
输出：`SyncReport`（扫描 / 新增 / 更新 / 未变 / 删除计数 + 摘要文本）。

口径（决定增量判断的上限）：
1. 新鲜度 = 文件 `mtime + size`（`fingerprint`）；只改旁车说明、不改素材文件不算变化；
2. 素材内容变化时清空 `embedding` / `embedding_model`，等向量回填（S2.5）补齐——
   宁可暂时缺向量，也不拿旧向量当新素材的相似度；
3. 素材目录不存在时**直接报错**（配置问题，不降级）——绝不因为"目录读不到"就清空索引；
4. 磁盘上消失的素材按 `path` 删行，`run_matches` 的外键是 `ON DELETE CASCADE`，会一并清理；
5. `plan_sync` 是纯函数（只读文件系统、不碰数据库），增量判断逻辑可以离线单测。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import PROJECT_ROOT, AppConfig, ConfigError
from ..db.models import Material as MaterialRow
from ..tools import materials as materials_tool


@dataclass
class SyncPlan:
    """增量计划：四类素材的绝对路径清单（已排序，便于断言与复现）。"""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def counts(self) -> tuple[int, int, int, int]:
        return len(self.added), len(self.updated), len(self.unchanged), len(self.deleted)


@dataclass
class SyncReport:
    """同步结果：写入数据库之后的计数报告。"""

    scanned: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    vision_used: int = 0

    def summary(self) -> str:
        return (f"素材同步：扫描 {self.scanned}、新增 {self.added}、更新 {self.updated}、"
                f"未变 {self.unchanged}、删除 {self.deleted}（视觉打标 {self.vision_used} 条）")


def _is_under(path: str, root: str) -> bool:
    """`path` 是否位于 `root` 之下（`root` 自身不算）。"""
    abs_path = os.path.abspath(path)
    abs_root = os.path.abspath(root)
    return abs_path != abs_root and abs_path.startswith(abs_root + os.sep)


def plan_sync(disk_entries: list[str], db_rows: list, materials_dir: str) -> SyncPlan:
    """规划增量：纯函数，只读文件系统、不碰数据库。

    输入：`disk_entries`（本次扫描到的素材文件路径）、`db_rows`（`materials` 表里的行，
    需要 `path` 与 `fingerprint` 两个属性）、`materials_dir`（本次扫描目录）。
    规则：`fingerprint` 为空的历史行按「需要重建」处理；只删除位于 `materials_dir` 之下的行；
    磁盘扫描为空时**不删任何行**（避免目录读不到时误清索引）。
    输出：`SyncPlan`（added / updated / unchanged / deleted，均为绝对路径并按字典序排序）。
    """
    disk_map = {os.path.abspath(p): materials_tool.fingerprint(p) for p in disk_entries}
    db_map = {os.path.abspath(row.path): row.fingerprint
              for row in db_rows if _is_under(row.path, materials_dir)}

    added, updated, unchanged = [], [], []
    for path in sorted(disk_map):
        disk_fp = disk_map[path]
        if path not in db_map:
            added.append(path)
        elif db_map[path] and db_map[path] == disk_fp:
            unchanged.append(path)
        else:
            # 内容变了，或历史行没有 fingerprint（需要重建）
            updated.append(path)

    deleted = [] if not disk_map else sorted(p for p in db_map if p not in disk_map)
    return SyncPlan(added=added, updated=updated, unchanged=unchanged, deleted=deleted)


def _relative_keyframes(keyframes: list[str]) -> list[str]:
    """把关键帧路径统一存成「相对工程根 + 正斜杠」，跨机器可读。"""
    out = []
    for path in keyframes:
        rel = path
        if os.path.isabs(path):
            try:
                rel = os.path.relpath(path, PROJECT_ROOT)
            except ValueError:
                # 关键帧目录与工程根不在同一个盘符（少见）：退回绝对路径，保证仍能定位文件
                rel = path
        out.append(rel.replace(os.sep, "/"))
    return out


def _row_values(material, keyframes: list[str]) -> dict:
    """把内存 `Material` 映射成 `materials` 表的列值（id 由数据库生成）。"""
    mtime = (datetime.fromtimestamp(material.mtime, tz=UTC)
             if material.mtime else None)
    return {
        "path": material.path,
        "type": material.type,
        "title": material.title,
        "description": material.description,
        "tags": list(material.tags),
        "elements": [element.to_dict() for element in material.elements],
        "source": material.source,
        "duration_s": Decimal(str(round(material.duration_s, 2))),
        "width": material.width,
        "height": material.height,
        "has_audio": material.has_audio,
        "size_bytes": material.size_bytes,
        "mtime": mtime,
        "keyframes": keyframes,
        "fingerprint": material.fingerprint,
        "indexed_at": datetime.now(UTC),
    }


async def sync_materials(session: AsyncSession, cfg: AppConfig, *,
                         vision=None, max_vision_items: int = 50) -> SyncReport:
    """把素材目录增量同步进 `materials` 表，返回计数报告。

    输入：`session`（异步会话）、`cfg`（读 `[paths]` 的素材 / 索引目录）、
    `vision`（视觉打标可调用对象，None = 不打标）、`max_vision_items`（本次打标上限）。
    行为：扫描 → 规划 → 对新增/变更文件调 `tools.materials.build_material` → 写入/更新/删除 → 提交。
    异常：素材目录不存在时抛 `ConfigError`（附修复提示）；数据库错误原样上抛。
    """
    materials_dir = cfg.materials_dir()
    if not os.path.isdir(materials_dir):
        raise ConfigError(
            f"素材目录不存在：{materials_dir}",
            "把素材放进 data/materials/，或在 config/config.toml 的 [paths].materials_dir "
            "指向真实目录；同步不会因为目录读不到就清空索引")

    keyframes_dir = os.path.join(cfg.index_dir(), "keyframes")
    disk_files = materials_tool.scan_materials(materials_dir)

    rows = list((await session.execute(select(MaterialRow))).scalars().all())
    plan = plan_sync(disk_files, rows, materials_dir)
    rows_by_path = {os.path.abspath(row.path): row
                    for row in rows if _is_under(row.path, materials_dir)}

    report = SyncReport(scanned=len(disk_files), unchanged=len(plan.unchanged),
                        deleted=len(plan.deleted))
    for path in plan.added + plan.updated:
        allow_vision = vision if report.vision_used < max_vision_items else None
        material = materials_tool.build_material(path, materials_dir, keyframes_dir,
                                                 vision=allow_vision)
        if allow_vision is not None and material.source == "vision":
            report.vision_used += 1
        values = _row_values(material, _relative_keyframes(material.keyframes))
        existing = rows_by_path.get(os.path.abspath(path))
        if existing is None:
            session.add(MaterialRow(**values))
            report.added += 1
        else:
            for column, value in values.items():
                setattr(existing, column, value)
            # 内容变了：旧向量不再可信，清空等回填（检索契约 §三 把 NULL 定义为「数据缺失」）
            existing.embedding = None
            existing.embedding_model = None
            report.updated += 1

    for path in plan.deleted:
        row = rows_by_path.get(os.path.abspath(path))
        if row is not None:
            await session.delete(row)

    await session.commit()
    return report
