"""历史关键帧的安全读取、内容摘要与运行范围 URL。

输入帧路径、允许缓存根与工程根；只读文件，不访问数据库或模型。链接、越界、缺失、
读取期间变化统一抛 ValueError；摘要来自实际返回的同一份 bytes，不把路径摘要当内容摘要。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .material_paths import is_link

_RUN_PREVIEW = re.compile(
    r"^/api/runs/[0-9a-fA-F-]{36}/hotspots/[1-9][0-9]*/candidates/[1-9][0-9]*/keyframes/[0-9]+$"
)


def read_frame(path: str, *, keyframes_root: str, project_root: str) -> bytes:
    """仅从指定缓存根读取无链接的普通文件；校验前不 resolve 以免掩盖链接。"""
    base = Path(os.path.abspath(keyframes_root))
    target = Path(os.path.abspath(Path(project_root, path)))
    try:
        relative = target.relative_to(base)
    except ValueError as exc:
        raise ValueError("keyframe_path_invalid") from exc
    cursor = base
    if any(is_link(parent) for parent in (base, *base.parents)):
        raise ValueError("keyframe_path_invalid")
    for part in relative.parts:
        cursor /= part
        if is_link(cursor):
            raise ValueError("keyframe_path_invalid")
    try:
        if not target.is_file() or not target.resolve().is_relative_to(base.resolve()):
            raise ValueError("keyframes_missing")
        with target.open("rb") as stream:
            before = os.fstat(stream.fileno())
            content = stream.read()
            after = os.fstat(stream.fileno())
        if ((before.st_size, before.st_mtime_ns, before.st_ino)
                != (after.st_size, after.st_mtime_ns, after.st_ino)
                or len(content) != after.st_size):
            raise ValueError("keyframe_content_changed")
        return content
    except OSError as exc:
        raise ValueError("keyframes_missing") from exc


def frame_content_records(frames: list[str], *, keyframes_root: str,
                          project_root: str) -> list[dict[str, Any]]:
    """捕获当前可安全读取帧；不可读取帧不生成虚假摘要。"""
    records = []
    for path in dict.fromkeys(frames):
        try:
            content = read_frame(path, keyframes_root=keyframes_root, project_root=project_root)
        except ValueError:
            continue
        records.append({"path": path, "sha256": hashlib.sha256(content).hexdigest(),
                        "size_bytes": len(content)})
    return records


def run_keyframe_url(run_id: str, position: int, rank: int, index: int = 0) -> str:
    return f"/api/runs/{run_id}/hotspots/{position}/candidates/{rank}/keyframes/{index}"


def safe_preview_url(value: Any) -> str:
    """报告只允许服务端运行路由；不接受文件、远程地址、data URL 或任意路径。"""
    return value if isinstance(value, str) and _RUN_PREVIEW.fullmatch(value) else ""
