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
import json
import os
import uuid
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, TypeVar

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import PROJECT_ROOT, AppConfig, ConfigError
from ..core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
)
from ..db.models import Material as MaterialRow
from ..schemas import Material
from ..tools import materials as materials_tool
from ..tools import media as media_tool
from ..tools.embedding import (
    EmbeddingClient,
    EmbeddingError,
    build_embedder,
    material_embedding_text,
)
from ..tools.http import build_http_client
from ..tools.material_paths import is_link, material_key, resolve_material, validate_key
from .material_identity import current_material, lock_material_index


@dataclass
class SyncPlan:
    """增量计划：四类素材根相对键（排序）；文件操作时单独解析本机路径。"""

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
    disk_map = {material_key(p, materials_dir): (material_key(p, materials_dir),
                                               materials_tool.fingerprint(p))
                for p in disk_entries}
    db_map = {validate_key(row.path): (row.path, row.fingerprint) for row in db_rows}
    keys: dict[str, str] = {}
    for key in [*disk_map, *db_map]:
        previous = keys.setdefault(key.casefold(), key)
        if previous != key:
            raise ValueError("素材键存在大小写歧义，拒绝同步")

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
    """把关键帧路径统一存成「相对工程根 + 正斜杠」，跨机器可读。

    索引目录不在工程根下时（临时目录 / 其他盘）**保留绝对路径**：不写 `../` 逃逸路径，
    也不把工程根相对规则套到素材根上。
    """
    out = []
    for path in keyframes:
        rel = path
        if os.path.isabs(path):
            try:
                candidate = os.path.relpath(path, PROJECT_ROOT)
            except ValueError:
                # 关键帧目录与工程根不在同一个盘符：保留绝对路径
                candidate = path
            if not os.path.isabs(candidate) and not candidate.startswith(".." + os.sep) \
                    and candidate != "..":
                rel = candidate
        out.append(rel.replace(os.sep, "/"))
    return out


def _row_values(material: Material, keyframes: list[str]) -> dict[str, Any]:
    """把内存 `Material` 映射成 `materials` 表的列值（id 由数据库生成）。"""
    mtime = (datetime.fromtimestamp(material.mtime, tz=UTC)
             if material.mtime else None)
    return {
        "path": validate_key(material.path),
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


def _insert_row(values: dict[str, Any]) -> Any:
    """按 `path` 幂等插入素材行（S6.5 冒烟抓到的并发问题）。

    为什么不是 `session.add`：素材同步是「扫目录 → 逐个文件重建」，而两个任务可能同时跑
    （多个上传各投一个索引任务、或分析与索引同时做新鲜度同步）。同一个新文件被两边同时插入时，
    唯一键 `materials.path` 会直接报 `IntegrityError`——整批任务就此失败。
    改成 `ON CONFLICT (path) DO UPDATE` 之后，后到的那次改写同一行（内容一致），
    并把向量清空等回填（与"内容变了"同一条规则）。
    """
    statement = pg_insert(MaterialRow).values(**values)
    return statement.on_conflict_do_update(
        index_elements=[MaterialRow.path],
        set_={**values, "embedding": None, "embedding_model": None})


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


# ---------- 素材库查询（S6.1：浏览 / 主题筛选 / 分页） ----------

MATERIALS_PAGE_DEFAULT = 24
MATERIALS_PAGE_MAX = 100


def relative_dir(path: str, materials_dir: str) -> str:
    """素材相对 `materials_dir` 的父目录（正斜杠；直接放在根目录下时为 `""`）。"""
    return validate_key(path).rpartition("/")[0]


def _relative_path_expr(materials_dir: str) -> Any:
    """SQL 里取「相对 materials_dir 的路径」（含文件名）。"""
    return MaterialRow.path


def _escape_like(value: str) -> str:
    """转义 LIKE 元字符：用户搜 `%` / `_` 时不该变成通配符。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _keyword_clause(keyword: str) -> Any:
    """关键词命中标题或任一标签（大小写不敏感的子串）。"""
    like = f"%{_escape_like(keyword)}%"
    return or_(MaterialRow.title.ilike(like, escape="\\"),
               func.array_to_string(MaterialRow.tags, ",").ilike(like, escape="\\"))


def _dir_clause(materials_dir: str, dir_value: str) -> Any:
    """`dir` 筛选：空串 = 只看直接放在根目录下的素材，非空 = 该目录前缀（含子目录）。"""
    rel = _relative_path_expr(materials_dir)
    normalized = dir_value.strip().strip("/")
    if not normalized:
        return func.strpos(rel, "/") == 0
    return func.substr(rel, 1, len(normalized) + 1) == normalized + "/"


def _filter_clauses(materials_dir: str, *, q: str | None, type: str | None,
                    source: str | None, dir: str | None) -> list[Any]:
    clauses: list[Any] = []
    keyword = (q or "").strip()
    if keyword:
        clauses.append(_keyword_clause(keyword))
    if type:
        clauses.append(MaterialRow.type == type)
    if source:
        clauses.append(MaterialRow.source == source)
    if dir is not None:
        clauses.append(_dir_clause(materials_dir, dir))
    return clauses


def _iso(value: Any) -> str:
    return value.isoformat() if value else ""


def material_item_view(row: MaterialRow, materials_dir: str) -> dict[str, Any]:
    """ORM 行 → 契约 `MaterialItem` 形状（`dir` 由绝对路径现算，库里没有这一列）。"""
    return {
        "id": str(row.id),
        "path": row.path,
        "type": row.type,
        "title": row.title or "",
        "description": row.description or "",
        "tags": list(row.tags or []),
        "duration_s": float(row.duration_s or 0),
        "width": int(row.width or 0),
        "height": int(row.height or 0),
        "has_audio": bool(row.has_audio),
        "size_bytes": int(row.size_bytes or 0),
        "source": row.source or "filename",
        "keyframes": list(row.keyframes or []),
        "indexed_at": _iso(row.indexed_at),
        "dir": relative_dir(row.path, materials_dir),
    }


async def list_materials(session: AsyncSession, cfg: AppConfig, *, q: str | None = None,
                         type: str | None = None, source: str | None = None,
                         dir: str | None = None,
                         limit: int = MATERIALS_PAGE_DEFAULT,
                         offset: int = 0) -> dict[str, Any]:
    """素材库列表：分页 + 关键词 / 类型 / 来源 / 主题目录筛选 + 主题目录计数（S6.1）。

    排序与分页口径：`indexed_at` 倒序、平局按 `id` 倒序（幂等）。`dirs` 只受
    `q` / `type` / `source` 影响——否则选中一个主题后其它主题的计数会集体消失。
    """
    materials_dir = cfg.materials_dir()
    base_clauses = _filter_clauses(materials_dir, q=q, type=type, source=source, dir=None)
    clauses = list(base_clauses)
    if dir is not None:
        clauses.append(_dir_clause(materials_dir, dir))

    total_stmt = select(func.count()).select_from(MaterialRow)
    if clauses:
        total_stmt = total_stmt.where(*clauses)
    total = int((await session.execute(total_stmt)).scalar_one())

    items_stmt = select(MaterialRow)
    if clauses:
        items_stmt = items_stmt.where(*clauses)
    items_stmt = (items_stmt.order_by(MaterialRow.indexed_at.desc(), MaterialRow.id.desc())
                  .limit(limit).offset(offset))
    rows = (await session.execute(items_stmt)).scalars().all()

    rel = _relative_path_expr(materials_dir)
    direct_dir = func.split_part(rel, "/", 1)
    dirs_stmt = select(direct_dir.label("path"), func.count().label("count")).where(
        func.strpos(rel, "/") > 0)
    if base_clauses:
        dirs_stmt = dirs_stmt.where(*base_clauses)
    dirs_stmt = dirs_stmt.group_by(direct_dir).order_by(direct_dir)
    dirs = [{"path": str(path), "count": int(count)}
            for path, count in (await session.execute(dirs_stmt)).all()]

    return {"items": [material_item_view(row, materials_dir) for row in rows],
            "total": total, "limit": limit, "offset": offset, "dirs": dirs}


async def load_material(session: AsyncSession, cfg: AppConfig,
                        material_id: uuid.UUID) -> dict[str, Any] | None:
    """单个素材的详情（列表项字段 + 爆点要素 `elements`）；不存在返回 None。"""
    row = await current_material(session, material_id)
    if row is None:
        return None
    view = material_item_view(row, cfg.materials_dir())
    view["elements"] = [dict(element) for element in (row.elements or [])]
    return view


# ---------- 素材库编辑（S6.2：人工确认过的元数据写回旁车 + 数据库） ----------

MAX_TAGS = 14
MAX_TAG_CHARS = 24


def single_line(value: str) -> str:
    """折成单行：旁车是 `键: 值` 行格式，标题 / 描述里的换行会把文件写坏。"""
    return " ".join(str(value).split())


def clean_tags(raw: Sequence[str]) -> list[str]:
    """去空白、去 `#` 前缀、去重（保序）——与 `tools/materials._merge_tags` 同一口径。"""
    out: list[str] = []
    for tag in raw:
        value = single_line(str(tag)).lstrip("#").strip()
        if value and value not in out:
            out.append(value)
    return out


def tag_problem(tags: Sequence[str]) -> str | None:
    """标签超限提示（None = 合规）：去重后最多 14 个、单个最多 24 字。"""
    if len(tags) > MAX_TAGS:
        return f"标签最多 {MAX_TAGS} 个（当前 {len(tags)} 个）"
    too_long = next((tag for tag in tags if len(tag) > MAX_TAG_CHARS), None)
    if too_long is not None:
        return f"单个标签最多 {MAX_TAG_CHARS} 字：{too_long[:MAX_TAG_CHARS]}"
    return None


def sidecar_target(media_path: str) -> str:
    """素材的 `.txt` 旁车路径（`sidecar_path` 优先读它，因此它优先于既有 `.md` / `.json`）。"""
    return media_path + ".txt"


def render_sidecar(*, title: str, tags: Sequence[str], description: str) -> str:
    """渲染旁车正文；`tools/materials.parse_sidecar` 能把这三行原样读回。"""
    return "\n".join([
        f"标题: {single_line(title)}",
        f"标签: {', '.join(tags)}",
        f"描述: {single_line(description)}",
    ]) + "\n"


def write_sidecar(media_path: str, *, title: str, tags: Sequence[str],
                  description: str, root: str | None = None) -> str:
    """把人工确认过的三个字段写进 `<素材文件名>.txt`（UTF-8 + LF），返回写到的路径。

    `root` 给了就按素材根重新解析旁车目标（拒绝根以下链接与越界），不能只信任调用方
    传入的媒体绝对路径——旁车本身也可能是指向别处的符号链接。
    """
    target = sidecar_target(media_path)
    if root is not None:
        key = validate_key(material_key(media_path, root))
        target = resolve_material(root, f"{key}.txt")
    elif is_link(target):
        raise ValueError("旁车文件不能是符号链接或 junction")
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_sidecar(title=title, tags=tags, description=description))
    return target


async def update_material(session: AsyncSession, cfg: AppConfig, material_id: uuid.UUID, *,
                          title: str | None = None, tags: list[str] | None = None,
                          description: str | None = None) -> dict[str, Any] | None:
    """改标题 / 标签 / 描述（S6.2）：先写旁车再落库，两边保持同一份事实。

    口径：

    - 三个字段的值都没变时**不写文件**（避免"只读一遍再保存"凭空多出 txt）；
    - 真的改了就把 `source` 标成 `sidecar`——线索已经由人工确认，重扫描会得到同样结论；
    - 素材文件已不在磁盘上时只更新数据库（不写孤立的 `.txt`，下次扫描会清理这行）；
    - `tags` 传 None = 不改；传空列表 = 清空标签；传值会先清洗（去空白 / 去 `#` / 去重），
      超过 14 个或单个超过 24 字抛 `BadRequestError`（400）——校验在查库之前，
      请求体不合法时不因为 id 是否存在而改变错误码。
    """
    cleaned_tags: list[str] | None = None
    if tags is not None:
        cleaned_tags = clean_tags(tags)
        problem = tag_problem(cleaned_tags)
        if problem is not None:
            raise BadRequestError(problem, {"tags": cleaned_tags})

    row = await current_material(session, material_id)
    if row is None:
        return None

    new_title = (row.title or "") if title is None else single_line(title)
    new_tags = list(row.tags or []) if cleaned_tags is None else cleaned_tags
    new_description = (row.description or "") if description is None else single_line(description)
    changed = (new_title, new_tags, new_description) != (
        row.title or "", list(row.tags or []), row.description or "")

    if changed:
        media_path = resolve_under(cfg.materials_dir(), row.path)
        if os.path.isfile(media_path):
            try:
                await asyncio.to_thread(
                    write_sidecar, media_path, title=new_title, tags=new_tags,
                    description=new_description, root=cfg.materials_dir())
            except (ValueError, OSError) as exc:
                raise BadRequestError(
                    "旁车文件不合法：不能是链接或越出素材根",
                    {"path": row.path}) from exc
        row.title, row.tags, row.description = new_title, new_tags, new_description
        row.source = "sidecar"
        await session.commit()

    detail = material_item_view(row, cfg.materials_dir())
    detail["elements"] = [dict(element) for element in (row.elements or [])]
    return detail


# ---------- 回收站（S6.3：移入 / 列出 / 恢复 / 真删） ----------

TRASH_DIR_NAME = "_trash"
COMPANION_SUFFIXES = (".txt", ".md", ".json")


def trash_root(cfg: AppConfig) -> str:
    """回收站根目录的**声明位置**（不解析链接）。

    文件操作一律走 `_trash_base` / `_trash_item`：`_trash` 自身或任意子层是链接时明确失败，
    不能先把 `_trash` 解析到外部目录、再把外部目录当成合法根。
    """
    return os.path.join(cfg.materials_dir(), TRASH_DIR_NAME)


def _trash_base(cfg: AppConfig) -> str:
    """从可信素材根解析 `_trash`；`_trash` 自身是链接 / 越界即失败。"""
    try:
        return resolve_material(cfg.materials_dir(), TRASH_DIR_NAME)
    except (ValueError, OSError) as exc:
        raise BadRequestError(
            "回收站目录不合法：不能是链接或越出素材根", {"path": TRASH_DIR_NAME}
        ) from exc


def _trash_item(cfg: AppConfig, relative: str) -> str:
    """把回收站相对路径解析成绝对路径；`_trash` 及每一层都按素材根重新校验。"""
    try:
        validate_key(relative)
        return resolve_material(cfg.materials_dir(), f"{TRASH_DIR_NAME}/{relative}")
    except (ValueError, OSError) as exc:
        raise BadRequestError(
            "回收站路径不合法：必须位于回收站内且不能包含链接",
            {"path": relative},
        ) from exc


def _materials_item(cfg: AppConfig, relative: str) -> str:
    try:
        return resolve_material(cfg.materials_dir(), relative)
    except (ValueError, OSError) as exc:
        raise BadRequestError(
            "路径不合法：必须位于素材根内且不能包含链接", {"path": relative}
        ) from exc


def resolve_under(root: str, relative: str) -> str:
    """把相对路径解析成 `root` 之下的绝对路径；空路径 / 绝对路径 / 越界一律 400。

    注意：`root` 必须是**可信根**（配置素材根）。回收站操作请用 `_trash_item`，
    不能把 `_trash` 自己当 root。
    """
    try:
        return resolve_material(root, relative)
    except (ValueError, OSError) as exc:
        raise BadRequestError("路径不合法：必须位于允许目录内且不能包含链接") from exc


def unique_path(target: str) -> str:
    """同名时自动加序号：`a.mp4` → `a-2.mp4` → `a-3.mp4`（返回第一个空位）。"""
    if not os.path.exists(target):
        return target
    stem, ext = os.path.splitext(target)
    index = 2
    while os.path.exists(f"{stem}-{index}{ext}"):
        index += 1
    return f"{stem}-{index}{ext}"


def _suffixes(source: str, companions: Sequence[str]) -> list[str]:
    return [""] + [path[len(source):] for path in companions]


def _choose_group_target(base_target: str, suffixes: Sequence[str]) -> str:
    """为**整个文件组**选一个无冲突名称（不能只检查媒体文件）。"""

    def free(candidate: str) -> bool:
        return all(not os.path.lexists(candidate + suffix) for suffix in suffixes)

    if free(base_target):
        return base_target
    stem, ext = os.path.splitext(base_target)
    index = 2
    while not free(f"{stem}-{index}{ext}"):
        index += 1
    return f"{stem}-{index}{ext}"


def companion_paths(media_path: str) -> list[str]:
    """媒体文件旁的说明文件（`<素材>.txt|.md|.json`）：跟着素材一起进出回收站。

    根以下的链接 / junction 明确失败（不能借旁车把操作引到素材根之外）。
    """
    candidates = [media_path + suffix for suffix in COMPANION_SUFFIXES]
    for path in candidates:
        if is_link(path):
            raise BadRequestError(
                "旁车文件不能是符号链接或 junction", {"path": os.path.basename(path)})
    return [path for path in candidates if os.path.isfile(path)]


def _record_fs_failure(cfg: AppConfig, payload: dict[str, Any]) -> str:
    """把无法自动补偿的文件操作记录到忽略目录；返回日志路径。"""
    directory = os.path.join(cfg.runs_dir(), "_material_identity")
    path = os.path.join(directory, "fs_failures.jsonl")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": datetime.now(UTC).isoformat(), **payload},
                                    ensure_ascii=False) + "\n")
    except OSError:  # pragma: no cover - 日志写失败不掩盖原始错误
        pass
    return path


def _manual_review(cfg: AppConfig, *, action: str,
                   pairs: Sequence[tuple[str, str]],
                   moved: Sequence[tuple[str, str]],
                   failures: Sequence[str]) -> ConflictError:
    log_path = _record_fs_failure(cfg, {
        "action": action,
        "planned": [{"source": source, "target": target} for source, target in pairs],
        "moved": [{"source": source, "target": target} for source, target in moved],
        "compensation_failures": list(failures),
        "manual_review_required": True,
    })
    return ConflictError(
        "文件组操作失败且无法自动恢复原状；已保留操作记录，需要人工核对后再重试",
        {"reason": "manual_review_required", "manual_review_required": True,
         "log": log_path},
    )


def _rollback(moved: Sequence[tuple[str, str]]) -> list[str]:
    """逆序把已移动的文件移回；返回无法恢复的描述。"""
    failures: list[str] = []
    for source, target in reversed(list(moved)):
        try:
            if os.path.lexists(source):
                failures.append(f"原位置已被占用：{source}")
                continue
            if not os.path.lexists(target):
                failures.append(f"目标已不存在：{target}")
                continue
            os.makedirs(os.path.dirname(source), exist_ok=True)
            os.replace(target, source)
        except OSError as exc:
            failures.append(f"{source}: {exc}")
    return failures


def _move_group(
    cfg: AppConfig,
    *,
    action: str,
    pairs: Sequence[tuple[str, str]],
    validate: Callable[[str], Any],
) -> list[tuple[str, str]]:
    """按顺序移动整组；任一步失败即逆序补偿，补偿失败抛人工核对错误。"""
    # 在第一次修改前留下完整意图。中断后可对照源、目标和数据库定位残留；
    # 这个记录不是提交凭证，不据此自动重放文件操作。
    for _source, target in pairs:
        validate(target)
        if os.path.lexists(target):
            raise FileExistsError(target)
    journal_dir = os.path.join(cfg.runs_dir(), "_material_identity", "fs_operations")
    os.makedirs(journal_dir, exist_ok=True)
    operation_id = str(uuid.uuid4())
    with open(os.path.join(journal_dir, operation_id + ".json"), "x", encoding="utf-8") as handle:
        json.dump({"operation_id": operation_id, "action": action,
                   "at": datetime.now(UTC).isoformat(), "evidence": "intent_only",
                   "planned": [{"source": source, "target": target} for source, target in pairs]},
                  handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    moved: list[tuple[str, str]] = []
    try:
        for source, target in pairs:
            validate(target)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            validate(target)
            if os.path.lexists(target):
                raise FileExistsError(target)
            os.replace(source, target)
            moved.append((source, target))
    except BaseException:
        failures = _rollback(moved)
        if failures:
            raise _manual_review(cfg, action=action, pairs=pairs, moved=moved,
                                 failures=failures) from None
        raise
    return moved


def _remove_empty_dirs(root: str) -> None:
    """清掉回收站里的空目录（自底向上）；root 自身保留；不跟随链接。"""
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        if os.path.realpath(dirpath) == os.path.realpath(root):
            continue
        if is_link(dirpath):
            continue
        with suppress(OSError):        # 非空目录 / 权限问题：留着即可，不算错误
            os.rmdir(dirpath)


async def trash_material(session: AsyncSession, cfg: AppConfig,
                         material_id: uuid.UUID) -> dict[str, Any] | None:
    """把素材（连同旁车）移进 `_trash/<原相对路径>`，再删掉索引行（S6.3）。

    口径：

    - 文件不在磁盘上时**仍然删行**（`file_missing=true`），不留删不掉的僵尸行；
    - 媒体与旁车作为一个文件组：整组选无冲突名称、先预检再移动、失败逆序补偿；
    - 数据库提交失败时把已移动文件移回；补偿失败明确返回人工核对并记录；
    - 删除 `materials` 行会通过外键 CASCADE 清掉 `run_matches`——**历史候选不再回来**，
      这是「恢复 = 重新入库（新 uuid）」的代价。
    """
    row = await current_material(session, material_id)
    if row is None:
        return None

    relative = row.path
    _trash_base(cfg)  # 缺失文件也先验证回收站边界，避免非法根时只删数据库。
    media_path = _materials_item(cfg, relative)
    file_missing = not os.path.isfile(media_path)
    trashed = relative.replace(os.sep, "/")
    moved: list[tuple[str, str]] = []
    if not file_missing:
        companions = companion_paths(media_path)
        sources = [media_path, *companions]
        base_target = _trash_item(cfg, relative)
        target = _choose_group_target(base_target, _suffixes(media_path, companions))
        pairs = [(source, target + source[len(media_path):]) for source in sources]
        root = _trash_base(cfg)
        trashed = os.path.relpath(target, root).replace(os.sep, "/")
        try:
            moved = _move_group(
                cfg, action="trash", pairs=pairs,
                validate=lambda path: _trash_item(
                    cfg, os.path.relpath(path, root).replace(os.sep, "/")
                ),
            )
        except FileExistsError as exc:
            raise ConflictError(
                f"回收站目标已存在：{relative}",
                {"reason": "trash_target_exists", "path": relative, "target": str(exc)},
            ) from exc
        except OSError as exc:
            raise ConflictError(
                "文件组移动失败，已恢复原状",
                {"reason": "file_group_move_failed", "path": relative,
                 "retryable": True},
            ) from exc

    material_id_str = str(row.id)
    try:
        await session.delete(row)
        await session.commit()
    except BaseException:
        try:
            await session.rollback()
        finally:
            if moved:
                failures = _rollback(moved)
                if failures:
                    raise _manual_review(cfg, action="trash_db_commit", pairs=moved,
                                         moved=moved, failures=failures) from None
        raise
    return {"material_id": material_id_str, "path": relative.replace(os.sep, "/"),
            "trashed_path": trashed, "file_missing": file_missing}


def list_trash(cfg: AppConfig) -> dict[str, Any]:
    """列出回收站里的**媒体**文件（旁车跟着媒体走，不单独列出），按相对路径排序。"""
    root = _trash_base(cfg)
    items: list[dict[str, Any]] = []
    if not os.path.isdir(root):
        return {"items": [], "total": 0}

    def onerror(exc: OSError) -> None:
        raise BadRequestError(
            f"回收站目录无法读取：{type(exc).__name__}",
            {"path": TRASH_DIR_NAME},
        ) from exc

    def checked(path: str) -> str:
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        _trash_item(cfg, rel)
        if is_link(path):
            raise BadRequestError(
                "回收站包含符号链接或 junction，已中止列举", {"path": rel})
        return rel

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        for name in dirnames + filenames:
            checked(os.path.join(dirpath, name))
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = os.path.join(dirpath, name)
            rel = checked(path)
            kind = media_tool.kind_of(path)
            if kind == "other":
                continue
            try:
                stat = os.stat(path)
            except OSError:  # pragma: no cover - 并发删除的窄窗口
                continue
            items.append({
                "path": rel,
                "name": name,
                "type": kind,
                "size_bytes": int(stat.st_size),
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            })
    items.sort(key=lambda item: item["path"])
    return {"items": items, "total": len(items)}


def restore_from_trash(cfg: AppConfig, relative: str) -> str:
    """把 `_trash/<relative>`（媒体 + 旁车）移回素材根目录，返回恢复后的绝对路径（S6.3）。

    恢复 = **重新入库**：调用方随后触发一次索引任务，拿到的是新 uuid、重新打标——
    `run_matches` 已在移入回收站时被级联清掉，不会回来。
    媒体或任一将写入的旁车目标已存在时，整组返回 409，保留双方原文件。
    """
    source = _trash_item(cfg, relative)
    if not os.path.isfile(source):
        raise NotFoundError(f"回收站里没有这个文件：{relative}", {"path": relative})
    companions = companion_paths(source)
    sources = [source, *companions]
    target = _materials_item(cfg, relative)
    pairs = [(item, target + item[len(source):]) for item in sources]
    conflicts = [
        os.path.relpath(dst, cfg.materials_dir()).replace(os.sep, "/")
        for _src, dst in pairs if os.path.lexists(dst)
    ]
    if conflicts:
        raise ConflictError(
            "目标位置已有同名文件，恢复未执行",
            {"reason": "restore_target_exists", "path": relative,
             "conflicts": conflicts},
        )
    try:
        _move_group(
            cfg, action="restore", pairs=pairs,
            validate=lambda path: _materials_item(
                cfg, os.path.relpath(path, cfg.materials_dir()).replace(os.sep, "/")
            ),
        )
    except FileExistsError as exc:  # 并发抢占：预检后、移动前
        raise ConflictError(
            "目标位置已有同名文件，恢复未执行",
            {"reason": "restore_target_exists", "path": relative,
             "target": str(exc)},
        ) from exc
    except OSError as exc:
        raise ConflictError(
            "文件组移动失败，已恢复原状",
            {"reason": "file_group_move_failed", "path": relative, "retryable": True},
        ) from exc
    return target


def purge_trash(cfg: AppConfig, *, paths: Sequence[str] | None = None,
                all_files: bool = False) -> dict[str, Any]:
    """真删回收站里的文件（S6.3）：`paths` 给具体条目（连同旁车），`all_files` 清空整个回收站。

    先整体预检（含链接 / 越界）再删：任何一条非法或不存在都不做部分删除。
    """
    root = _trash_base(cfg)
    if not os.path.isdir(root):
        return {"deleted": [], "count": 0}

    def onerror(exc: OSError) -> None:
        raise BadRequestError(
            f"回收站目录无法读取：{type(exc).__name__}", {"path": TRASH_DIR_NAME}
        ) from exc

    targets: list[str] = []
    if all_files:
        for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
            for name in dirnames + filenames:
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                _trash_item(cfg, rel)
                if is_link(path):
                    raise BadRequestError(
                        "回收站包含符号链接或 junction，清空已中止", {"path": rel})
            for name in filenames:
                targets.append(os.path.join(dirpath, name))
    else:
        for relative in paths or []:
            source = _trash_item(cfg, relative)
            if not os.path.isfile(source):
                raise NotFoundError(f"回收站里没有这个文件：{relative}",
                                    {"path": relative})
            group = [source, *companion_paths(source)]
            for path in group:
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                _trash_item(cfg, rel)
                if is_link(path):
                    raise BadRequestError(
                        "回收站文件不能是符号链接或 junction", {"path": rel})
            targets.extend(group)

    deleted: list[str] = []
    for target in dict.fromkeys(targets):          # 去重：all 模式下不会重复，单条模式可能
        relative = os.path.relpath(target, root).replace(os.sep, "/")
        try:
            os.remove(target)
        except FileNotFoundError:                  # pragma: no cover - 并发删除的窄窗口
            continue
        deleted.append(relative)
    _remove_empty_dirs(root)
    deleted.sort()
    return {"deleted": deleted, "count": len(deleted)}


# ---------- 素材上传（S6.4：流式落盘到 <YYYY-MM>/） ----------

UPLOAD_CHUNK_BYTES = 1024 * 1024
INCOMING_PREFIX = ".incoming-"
_WINDOWS_ILLEGAL = '<>:"|?*'


class _Upload(Protocol):
    """`fastapi.UploadFile` 的窄接口：服务层只需要文件名与分块读。"""

    filename: str | None

    async def read(self, size: int = ...) -> bytes: ...


def safe_filename(raw: str | None) -> str:
    """只取 basename 并清掉 Windows 非法字符；空名 / `.` / `..` → 400。"""
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    if not name or name in {".", ".."} or "\x00" in name:
        raise BadRequestError("文件名不合法", {"filename": raw or ""})
    for bad in _WINDOWS_ILLEGAL:
        name = name.replace(bad, "_")
    return name


def upload_dir(cfg: AppConfig, *, now: datetime | None = None) -> str:
    """上传落点：`<materials_dir>/<YYYY-MM>/`（数据契约 §一 的目录约定）。"""
    stamp = (now or datetime.now()).strftime("%Y-%m")
    return os.path.join(cfg.materials_dir(), stamp)


async def save_upload(cfg: AppConfig, upload: _Upload, *,
                      now: datetime | None = None) -> dict[str, Any]:
    """把一个上传的媒体文件流式写进 `<YYYY-MM>/`，返回 `{path, name, size_bytes}`（S6.4）。

    口径：

    - 扩展名不在 `[upload].allowed_extensions` → 400（**按文件名后缀判断**：素材入库后由抽帧与
      打标负责内容，这里不做 magic bytes 嗅探——那是热点图片那条链路的口径）；
    - 边写边计体积，超过 `[upload].max_size_gb` → 413，并删掉临时文件；
    - 空文件 → 400；
    - 先写 `<YYYY-MM>/.incoming-<uuid>.part` 再原子改名：中途失败不会在素材目录里留下半截文件，
      也不会被扫描当成素材（`.incoming-*` 以 `.` 开头，`scan_materials` 直接跳过）。
    """
    name = safe_filename(upload.filename)
    suffix = os.path.splitext(name)[1].lower()
    allowed = [str(ext).lower() for ext in cfg.upload.allowed_extensions]
    if suffix not in allowed:
        raise BadRequestError(f"不支持的文件类型：{suffix or '（没有扩展名）'}",
                              {"allowed_extensions": allowed})

    limit_bytes = int(cfg.upload.max_size_gb * 1024 ** 3)
    target_dir = upload_dir(cfg, now=now)
    await asyncio.to_thread(os.makedirs, target_dir, exist_ok=True)
    # 月份目录本身可能是预先放好的链接：落盘前按素材根重新解析，链接 / 越界明确失败
    month = os.path.basename(target_dir.rstrip("/\\"))
    try:
        resolve_material(cfg.materials_dir(), month)
    except (ValueError, OSError) as exc:
        raise BadRequestError("上传目录不合法：不能是链接或越出素材根") from exc
    temp = os.path.join(target_dir, f"{INCOMING_PREFIX}{uuid.uuid4().hex}.part")
    total = 0
    try:
        with open(temp, "wb") as handle:
            while True:
                chunk = await upload.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit_bytes:
                    raise PayloadTooLargeError(
                        f"文件超过 {cfg.upload.max_size_gb} GB 上限",
                        {"max_size_gb": cfg.upload.max_size_gb, "limit_bytes": limit_bytes})
                await asyncio.to_thread(handle.write, chunk)
        if total == 0:
            raise BadRequestError("上传的文件为空")
        final = unique_path(os.path.join(target_dir, name))
        await asyncio.to_thread(os.replace, temp, final)
    except BaseException:
        with suppress(OSError):
            os.remove(temp)
        raise

    return {"path": os.path.relpath(final, cfg.materials_dir()).replace(os.sep, "/"),
            "name": os.path.basename(final), "size_bytes": total}


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

    await lock_material_index(session)
    keyframes_dir = os.path.join(cfg.index_dir(), "keyframes")
    disk_files = materials_tool.scan_materials(materials_dir)

    # 规划只取两列：大库场景下不把 elements / keyframes / embedding 这些大列整行读进来
    plan_rows = (await session.execute(
        select(MaterialRow.path, MaterialRow.fingerprint))).all()
    plan = plan_sync(disk_files, list(plan_rows), materials_dir)

    # 只有要更新/删除的行才取回完整 ORM 对象（新增行本来就不在库里）
    rows_by_path: dict[str, MaterialRow] = {}
    touched = [*plan.updated, *plan.deleted]
    if touched:
        changed = (await session.execute(
            select(MaterialRow).where(MaterialRow.path.in_(touched)))).scalars().all()
        rows_by_path = {row.path: row for row in changed}

    report = SyncReport(scanned=len(disk_files), unchanged=len(plan.unchanged),
                        deleted=len(plan.deleted))
    for path in plan.added + plan.updated:
        allow_vision = vision if report.vision_used < max_vision_items else None
        # build_material 是同步函数（读文件 + 可能调多模态打标，单条可达数秒）：放线程里跑，
        # 避免在 Celery worker 的 async 上下文里阻塞事件循环（E3 审查建议 1）
        material = await asyncio.to_thread(
            materials_tool.build_material, resolve_material(materials_dir, path),
            materials_dir, keyframes_dir,
            vision=allow_vision)
        if allow_vision is not None and material.source == "vision":
            report.vision_used += 1
        values = _row_values(material, _relative_keyframes(material.keyframes))
        existing = rows_by_path.get(path)
        if existing is None:
            # 幂等插入：并发同步撞上同一路径时改写而不是报错（见 `_insert_row`）
            await session.execute(_insert_row(values))
            report.added += 1
        else:
            for column, value in values.items():
                setattr(existing, column, value)
            # 内容变了：旧向量不再可信，清空等回填（检索契约 §三 把 NULL 定义为「数据缺失」）
            existing.embedding = None
            existing.embedding_model = None
            report.updated += 1

    for path in plan.deleted:
        row = rows_by_path.get(path)
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

    # S3.5：未注入 embedder 时起一个客户端（连接池）并在这里关闭；注入的用自己的生命周期。
    async with AsyncExitStack() as stack:
        client: EmbeddingClient | None = embedder
        if client is None:
            http = await stack.enter_async_context(
                build_http_client(cfg.embedding.timeout_s))
            client = build_embedder(cfg, client=http)
        if client is None:
            raise ConfigError(
                "向量召回未启用（[embedding].enabled = false）",
                "把 config/config.toml 的 [embedding].enabled 设为 true 后再回填")

        for batch in _chunks(todo, batch_size or cfg.embedding.batch_size):
            try:
                result = await client.embed([text for _row, text in batch])
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
