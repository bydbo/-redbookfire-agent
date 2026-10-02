"""素材索引同步与向量回填：把素材目录的变化增量落进 `materials` 表，并补齐语义向量。

用途：① 一次增量同步——扫描目录 → 规划新增/更新/未变/删除 → 对变化的文件重建素材行并落库；
      ② `backfill_embeddings`——给缺向量或换了模型的素材补齐 `embedding` / `embedding_model`。
输入：`AsyncSession`、`AppConfig`；可选注入 `vision`（`(frames, hint) -> dict | None` 的
      视觉打标可调用对象）与 `max_vision_items`（本次最多打标多少条，控制成本）；回填可注入
      `embedder`（与 `tools.embedding.EmbeddingClient` 同形状，便于离线测试）。
输出：`SyncReport`（扫描 / 新增 / 更新 / 未变 / 删除）与 `EmbeddingBackfillReport`
      （待处理 / 写入 / 跳过空文本 / 失败 / 批次 / prompt_tokens），都带 `summary()`。

口径（决定增量判断的上限）：
1. 新鲜度 = 文件 `mtime + size`（`fingerprint`）；只改旁车说明、不改素材文件不算变化；
2. 素材内容变化时清空 `embedding` / `embedding_model`，等向量回填（S2.5）补齐——
   宁可暂时缺向量，也不拿旧向量当新素材的相似度；
3. 素材目录不存在时**直接报错**（配置问题，不降级）——绝不因为"目录读不到"就清空索引；
4. 磁盘上消失的素材按 `path` 删行，`run_matches` 的外键是 `ON DELETE CASCADE`，会一并清理；
5. `plan_sync` 是纯函数（只读文件系统、不碰数据库），增量判断逻辑可以离线单测；
6. 向量回填按批提交、失败即停、重跑幂等：只处理 `embedding IS NULL` 或 `embedding_model`
   与当前模型不符的行（换模型即重建），不修改 `indexed_at`。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, TypeVar

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import PROJECT_ROOT, AppConfig, ConfigError
from ..db.models import Material as MaterialRow
from ..schemas import Material
from ..tools import materials as materials_tool
from ..tools.embedding import (
    EmbeddingClient,
    EmbeddingError,
    build_embedder,
    material_embedding_text,
)


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


def _path_key(path: str) -> str:
    """路径比较键：绝对化 + `normcase`。

    Windows 的路径比较大小写不敏感，而字符串 `startswith` / 字典键是敏感的：统一走
    `normcase` 再比，避免同一路径只因盘符大小写不同就被当成两条素材（会写出重复行）。
    """
    return os.path.normcase(os.path.abspath(path))


def _is_under(path: str, root: str) -> bool:
    """`path` 是否位于 `root` 之下（`root` 自身不算）。"""
    key_path = _path_key(path)
    key_root = _path_key(root)
    return key_path != key_root and key_path.startswith(key_root + os.sep)


T = TypeVar("T")


class _SyncRow(Protocol):
    """`plan_sync` 需要的行属性（只依赖这两列，不绑定 ORM 类型）。"""

    path: str
    fingerprint: str | None


def plan_sync(disk_entries: list[str], db_rows: Sequence[_SyncRow],
              materials_dir: str) -> SyncPlan:
    """规划增量：纯函数，只读文件系统、不碰数据库。

    输入：`disk_entries`（本次扫描到的素材文件路径）、`db_rows`（`materials` 表里的行，
    需要 `path` 与 `fingerprint` 两个属性）、`materials_dir`（本次扫描目录）。
    规则：`fingerprint` 为空的历史行按「需要重建」处理；只删除位于 `materials_dir` 之下的行；
    磁盘扫描为空时**不删任何行**（避免目录读不到时误清索引）。
    输出：`SyncPlan`（added / updated / unchanged / deleted，均为绝对路径并按字典序排序）。
    """
    # 键用 normcase 后的路径比较；值保留原始绝对路径，供落库与报错使用
    disk_map = {_path_key(p): (os.path.abspath(p), materials_tool.fingerprint(p))
                for p in disk_entries}
    db_map = {_path_key(row.path): (os.path.abspath(row.path), row.fingerprint)
              for row in db_rows if _is_under(row.path, materials_dir)}

    added, updated, unchanged = [], [], []
    for key in sorted(disk_map, key=lambda item: disk_map[item][0]):
        path, disk_fp = disk_map[key]
        row = db_map.get(key)
        if row is None:
            added.append(path)
        elif row[1] and row[1] == disk_fp:
            unchanged.append(path)
        else:
            # 内容变了，或历史行没有 fingerprint（需要重建）
            updated.append(path)

    deleted = [] if not disk_map else sorted(
        path for key, (path, _fp) in db_map.items() if key not in disk_map)
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


def _row_values(material: Material, keyframes: list[str]) -> dict[str, Any]:
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


def row_to_material(row: MaterialRow) -> Material:
    """ORM 行 → 内存 `Material`（检索与解释共用）。

    输入：`materials` 表的一行。输出：`schemas.Material`——`id` 取数据库 uuid（与 API 契约的
    `format: uuid` 一致），`mtime` 由 timestamptz 转 epoch 秒，`elements` 复用 JSONB 结构。
    """
    return Material.from_dict({
        "id": str(row.id),
        "path": row.path,
        "type": row.type,
        "title": row.title,
        "description": row.description,
        "tags": list(row.tags or []),
        "elements": list(row.elements or []),
        "duration_s": float(row.duration_s or 0),
        "width": row.width,
        "height": row.height,
        "has_audio": row.has_audio,
        "size_bytes": row.size_bytes,
        "mtime": row.mtime.timestamp() if row.mtime else 0.0,
        "source": row.source,
        "keyframes": list(row.keyframes or []),
        "indexed_at": row.indexed_at.isoformat() if row.indexed_at else "",
        "fingerprint": row.fingerprint or "",
    })


async def sync_materials(session: AsyncSession, cfg: AppConfig, *,
                         vision: Callable[[list[str], str], dict[str, Any] | None] | None = None,
                         max_vision_items: int = 50) -> SyncReport:
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


@dataclass
class EmbeddingBackfillReport:
    """向量回填结果：给运维命令与上层编排看的计数报告。"""

    model: str = ""
    pending: int = 0
    embedded: int = 0
    skipped_empty: int = 0
    failed: int = 0
    batches: int = 0
    prompt_tokens: int = 0
    error: str = ""

    def summary(self) -> str:
        text = (f"向量回填（{self.model or '未启用'}）：待处理 {self.pending}、写入 {self.embedded}、"
                f"跳过空文本 {self.skipped_empty}、失败 {self.failed}、批次 {self.batches}、"
                f"prompt_tokens {self.prompt_tokens}")
        return f"{text}；错误：{self.error}" if self.error else text


def _chunks(items: Sequence[T], size: int) -> list[list[T]]:
    step = max(1, int(size))
    return [list(items[i:i + step]) for i in range(0, len(items), step)]


async def backfill_embeddings(session: AsyncSession, cfg: AppConfig, *,
                              embedder: EmbeddingClient | None = None,
                              batch_size: int | None = None,
                              limit: int | None = None) -> EmbeddingBackfillReport:
    """给缺向量或换了模型的素材回填 `embedding` / `embedding_model`，返回计数报告。

    输入：`session`（异步会话）、`cfg`（读 `[embedding]` 的模型 / 批量 / 超时 / 重试）、
    `embedder`（可注入的向量化对象，None = 按配置现造）、`batch_size`（覆盖配置的每批条数）、
    `limit`（本次最多处理多少条，便于分批运维）。
    行为：选行 → 拼文本 → 分批调用 → 成功批提交（失败批未写入，停止并报告）→ 返回报告。
    异常：`[embedding].enabled = false` 抛 `ConfigError`（提示先打开开关，不做静默空跑）。
    """
    if not cfg.embedding.enabled:
        raise ConfigError(
            "向量召回未启用（[embedding].enabled = false）",
            "把 config/config.toml 的 [embedding].enabled 设为 true 后再回填；"
            "索引同步不会隐式调用模型")
    client = embedder if embedder is not None else build_embedder(cfg)
    if client is None:
        raise ConfigError(
            "向量召回未启用（[embedding].enabled = false）",
            "把 config/config.toml 的 [embedding].enabled 设为 true 后再回填")

    model = cfg.embedding.model
    statement = (
        select(MaterialRow)
        .where(or_(MaterialRow.embedding.is_(None),
                   MaterialRow.embedding_model.is_distinct_from(model)))
        .order_by(MaterialRow.path)
    )
    if limit:
        statement = statement.limit(int(limit))
    rows = list((await session.execute(statement)).scalars().all())

    report = EmbeddingBackfillReport(model=model, pending=len(rows))
    todo: list[tuple[MaterialRow, str]] = []
    for row in rows:
        text = material_embedding_text(row.title, row.description, row.tags or [],
                                       row.elements or [])
        if text:
            todo.append((row, text))
        else:
            report.skipped_empty += 1

    for batch in _chunks(todo, batch_size or cfg.embedding.batch_size):
        try:
            # embed 是同步 urllib 调用，放线程里跑，避免阻塞事件循环（S3.5 换 httpx 后改回原生异步）
            result = await asyncio.to_thread(
                client.embed, [text for _row, text in batch])
        except EmbeddingError as exc:
            # 失败批尚未写入任何行，直接停止；已提交的批保留，重跑只补未完成的行
            report.failed = len(batch)
            report.error = str(exc)
            break
        if len(result.vectors) != len(batch):
            report.failed = len(batch)
            report.error = f"返回向量条数 {len(result.vectors)} 与请求 {len(batch)} 不符"
            break
        for (row, _text), vector in zip(batch, result.vectors, strict=True):
            row.embedding = vector
            row.embedding_model = model
        await session.commit()
        report.batches += 1
        report.embedded += len(batch)
        report.prompt_tokens += result.prompt_tokens

    return report
