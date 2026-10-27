"""历史预览读侧：当前素材状态、归属和内容版本校验；不回写历史记录。

只读取当前素材与安全缓存文件。read_candidate_frame 返回已校验 bytes，响应使用同一份
bytes，避免 FileResponse 在校验后重新打开被替换的文件。旧快照无内容摘要时明确不可确认。
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import PROJECT_ROOT, AppConfig
from ..core.errors import ConflictError, NotFoundError
from ..db.models import Material, Run, RunHotspot, RunMatch
from ..tools.historical_media import read_frame
from ..tools.material_paths import resolve_material
from .material_identity import canonical_id, current_material


def read_candidate_frame(material: Material | None, snapshot: dict[str, Any] | None,
                         cfg: AppConfig, index: int) -> bytes:
    if material is None:
        raise ValueError("material_deleted")
    if not snapshot:
        raise ValueError("preview_unverified")
    if str(snapshot.get("fingerprint") or "") != str(material.fingerprint or ""):
        raise ValueError("material_version_changed")
    frames = list(snapshot.get("keyframes") or [])
    if index < 0 or index >= len(frames) or frames[index] not in (material.keyframes or []):
        raise ValueError("keyframes_missing")
    expected = [entry for entry in snapshot.get("keyframe_contents") or []
                if isinstance(entry, dict) and entry.get("path") == frames[index]]
    if len(expected) != 1 or not expected[0].get("sha256"):
        raise ValueError("preview_unverified")
    content = read_frame(str(frames[index]), keyframes_root=str(Path(cfg.index_dir(), "keyframes")),
                         project_root=PROJECT_ROOT)
    if (hashlib.sha256(content).hexdigest() != expected[0]["sha256"]
            or len(content) != expected[0].get("size_bytes")):
        raise ValueError("keyframe_content_changed")
    return content


def current_material_status(material: Material | None, snapshot: dict[str, Any] | None,
                            cfg: AppConfig | None) -> dict[str, Any]:
    current: dict[str, Any] = {
        "exists": material is not None, "file_available": None,
        "preview_available": False, "reason": "material_deleted" if material is None else None,
        "path": material.path if material is not None else None,
        "title": material.title if material is not None else None,
    }
    if material is None:
        return current
    if cfg is None:
        current["reason"] = "preview_unverified"
        return current
    try:
        current["file_available"] = Path(resolve_material(cfg.materials_dir(), material.path)).is_file()
    except (OSError, ValueError):
        current["file_available"] = False
    try:
        read_candidate_frame(material, snapshot, cfg, 0)
    except ValueError as exc:
        current["reason"] = str(exc)
    else:
        current["preview_available"] = True
    return current


async def load_run_preview(session: AsyncSession, run_id: uuid.UUID, position: int,
                           rank: int, index: int, cfg: AppConfig) -> bytes:
    """按服务端历史身份定位帧；别名只证明历史 UUID 等价，不按路径寻找新素材。"""
    run = await session.get(Run, run_id)
    if run is None:
        raise NotFoundError("运行不存在", {"run_id": str(run_id)})
    hotspot = (await session.execute(select(RunHotspot).where(
        RunHotspot.run_id == run_id, RunHotspot.position == position))).scalar_one_or_none()
    if hotspot is None:
        raise NotFoundError("热点不存在", {"position": position})
    match = (await session.execute(select(RunMatch).where(
        RunMatch.run_hotspot_id == hotspot.id, RunMatch.rank == rank))).scalar_one_or_none()
    if match is None or not match.material_snapshot:
        raise NotFoundError("历史候选快照不可用", {"position": position, "rank": rank})
    snapshot = dict(match.material_snapshot)
    try:
        historical_id = uuid.UUID(str(snapshot.get("id") or ""))
    except ValueError as exc:
        raise ConflictError("历史素材身份无法确认", {"reason": "preview_unverified"}) from exc
    if await canonical_id(session, historical_id) != match.material_id:
        raise ConflictError("历史素材身份无法确认", {"reason": "preview_unverified"})
    current = await current_material(session, match.material_id)
    try:
        return read_candidate_frame(current, snapshot, cfg, index)
    except ValueError as exc:
        reason = str(exc)
        if reason in {"material_deleted", "keyframes_missing", "keyframe_path_invalid"}:
            raise NotFoundError("历史预览不可用", {"reason": reason, "index": index}) from exc
        raise ConflictError("历史预览无法确认", {"reason": reason}) from exc
