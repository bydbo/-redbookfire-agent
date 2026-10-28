"""E9 隔离实验的数据库编排：索引、真实拆解与四策略运行。

本模块只做编排；具体同步、向量化、拆解和检索复用现有服务。正式运行要求独立数据库，
CLI 负责用 Alembic 升级并注入 `DATABASE_URL`，绝不使用 `create_all` 冒充迁移。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents.clue import extract_clue
from ..config import AppConfig
from ..db.models import Material as MaterialRow
from ..schemas import HotspotClue
from ..services.image_clue import parse_image_clue, parse_image_clue_multi
from ..services.materials import backfill_embeddings, sync_materials
from ..services.run_snapshots import digest
from ..tools.embedding import EmbeddingClient
from ..tools.llm import StructuredCaller
from ..tools.vision import sniff_image_mime
from .models import Attempt, Case, Dataset
from .retrieval import ClueProvider, run_retrieval


class EvalPaths:
    """把实验目录包装成 AppConfig 的 paths 视图，其余字段透传。"""

    def __init__(self, base: AppConfig, materials_dir: Path, index_dir: Path) -> None:
        self._base = base
        self._materials_dir = materials_dir
        self._index_dir = index_dir

    def materials_dir(self) -> str:
        return str(self._materials_dir)

    def index_dir(self) -> str:
        return str(self._index_dir)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def library_digest(rows: Sequence[Any]) -> str:
    """素材身份 / 指纹 / 索引时间 / 向量模型的摘要，不包含 embedding 本体。"""
    payload = [{
        "id": str(row.id),
        "path": row.path,
        "fingerprint": row.fingerprint or "",
        "indexed_at": row.indexed_at.isoformat() if row.indexed_at else "",
        "embedding_model": row.embedding_model or "",
        "has_embedding": row.embedding is not None,
    } for row in rows]
    return digest(payload)


async def index_dataset(session: AsyncSession, cfg: AppConfig, dataset: Dataset,
                        root: Path, embedder: EmbeddingClient,
                        output_dir: Path) -> dict[str, Any]:
    """同步素材 + 回填向量 + 写索引摘要；缺任何素材或向量失败都直接失败。"""
    if not cfg.embedding.enabled:
        raise ValueError("index 需要启用 embedding；关闭状态不能形成可比较的向量索引")
    # 索引缓存不能放在素材根内：否则第二次扫描会把关键帧当成新素材。
    paths = EvalPaths(cfg, root, root.parent / f"{root.name}-index")
    await sync_materials(session, paths, vision=None)  # type: ignore[arg-type]
    report = await backfill_embeddings(session, paths, embedder=embedder)  # type: ignore[arg-type]
    if report.failed:
        raise RuntimeError(f"向量回填失败 {report.failed} 条：{report.error}")
    rows = list((await session.execute(
        select(MaterialRow).order_by(MaterialRow.path))).scalars().all())
    expected = {asset.relative_path for asset in dataset.materials}
    actual = {row.path.replace("\\", "/") for row in rows}
    missing = sorted(expected - actual)
    if missing:
        raise ValueError(f"索引缺少数据集素材：{missing[:5]}")
    extra = sorted(actual - expected)
    if extra:
        raise ValueError(f"隔离库含数据集之外素材：{extra[:5]}；请使用空库或重建")
    payload = {
        "materials_count": len(rows),
        "digest": library_digest(rows),
        "embedding_model": cfg.embedding.model,
        "prompt_tokens": report.prompt_tokens,
        "embedded": report.embedded,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    return payload


def _read_image(path: Path) -> tuple[bytes, str]:
    data = path.read_bytes()
    mime = sniff_image_mime(data)
    if not mime:
        raise ValueError(f"无法识别图片类型：{path}")
    return data, mime


def make_clue_provider(cfg: AppConfig, dataset: Dataset, root: Path,
                       http: Any, caller: StructuredCaller) -> ClueProvider:
    """构造真实拆解 provider：文本走 `extract_clue`，图片走真实视觉入口。"""
    async def provide(case: Case, repeat: int) -> HotspotClue:
        del repeat
        if case.input_type == "text":
            text = case.natural_input.text or case.expectation.summary
            outcome = await extract_clue(text, caller)
            return cast(HotspotClue, outcome.value)
        assets = {asset.material_key: asset for asset in dataset.materials}
        images = []
        for ref in case.natural_input.images:
            asset = assets.get(ref.material_key)
            if asset is None:
                raise ValueError(f"图片素材不存在：{ref.material_key}")
            images.append(_read_image(root / asset.relative_path))
        if len(images) == 1:
            parsed = await parse_image_clue(cfg, images[0][0], images[0][1], http=http)
        else:
            parsed = await parse_image_clue_multi(cfg, images, http=http)
        return parsed.clue
    return provide


async def run_split(session: AsyncSession, cfg: AppConfig, dataset: Dataset,
                    root: Path, cases: Sequence[Case], *, split: str,
                    embedder: EmbeddingClient, http: Any,
                    caller: StructuredCaller | None,
                    modes: Sequence[str] = ("oracle", "real"),
                    now: float | None = None
                    ) -> tuple[list[Attempt], dict[str, Any]]:
    """执行 oracle（1 次）和 / 或真实拆解（3 次）；返回全部尝试。"""
    if split not in ("dev", "holdout"):
        raise ValueError(f"未知分区：{split}")
    selected = [case for case in cases if case.split == split]
    attempts: list[Attempt] = []
    index: dict[str, Any] = {}
    if "oracle" in modes:
        oracle_attempts, oracle_index = await run_retrieval(
            session, cfg, dataset, selected, embedder=embedder, clue_mode="oracle",
            repeats=1, now=now)
        attempts.extend(oracle_attempts)
        index = oracle_index or index
    if "real" in modes:
        if caller is None:
            raise ValueError("真实拆解模式需要 StructuredCaller")
        provider = make_clue_provider(cfg, dataset, root, http, caller)
        real_attempts, real_index = await run_retrieval(
            session, cfg, dataset, selected, embedder=embedder, clue_mode="real",
            repeats=3, clue_provider=provider, now=now)
        attempts.extend(real_attempts)
        index = real_index or index
    return attempts, index
