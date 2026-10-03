"""素材库：扫描、旁车解析与单文件构造。

用途：把「一个素材文件」变成 `Material`——扫描目录、读旁车说明、按文件名兜底、
      探测媒体信息、抽关键帧、可选视觉打标；增量与落库由 `services/materials.py` 负责
      （S2.4 起数据库是唯一索引源，S2.10 起 JSON 索引已退役）。
输入：素材文件路径 + 素材根目录 + 关键帧目录；可选注入视觉打标可调用对象与抽帧参数。
输出：`Material`（`build_material`）或扫描到的媒体文件列表（`scan_materials`）。

索引约定（非常重要，决定了匹配质量的上下限）：
1. 同目录下 `素材名.mp4.txt` / `素材名.md` 会被当成人工说明，优先级最高；
2. 文件名里的 `-` `_` 分隔词会被当作标签；
3. 抽帧与视觉打标是分开的两件事：抽帧默认开启（报告要缩略图，`frame_count` /
   `frame_max_width` 可传参，也能用 `extract_frames=False` 整体跳过），
   开启视觉模型后才会把这些帧送去补描述。
"""

from __future__ import annotations

import os

from ..schemas import Element, Material
from ..util import now_iso, read_json, read_text
from . import lexicon, media

SIDECAR_EXT = {".txt", ".md", ".json"}
_TAG_KEYS = ("tags", "tag", "标签")
_TITLE_KEYS = ("title", "标题")
_DESC_KEYS = ("desc", "description", "描述", "说明", "内容")


def scan_materials(materials_dir: str, recursive: bool = True) -> list:
    """扫描素材目录，返回媒体文件路径列表（忽略说明文件和隐藏文件）。"""
    if not os.path.isdir(materials_dir):
        return []
    found = []
    walker = os.walk(materials_dir) if recursive else [(materials_dir, [], os.listdir(materials_dir))]
    for root, dirs, files in walker:
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith("_")]
        for name in sorted(files):
            if name.startswith(".") or name.startswith("~"):
                continue
            path = os.path.join(root, name)
            if media.kind_of(path) in {"video", "image"}:
                found.append(path)
    return found


def sidecar_path(media_path: str) -> str | None:
    for ext in (".txt", ".md", ".json"):
        candidate = media_path + ext
        if os.path.exists(candidate):
            return candidate
    return None


def parse_sidecar(path: str) -> dict:
    """解析人工说明文件：支持 `标签: a, b` 形式，也支持纯描述文本。"""
    result = {"title": "", "tags": [], "description": ""}
    if not path or not os.path.exists(path):
        return result
    if path.lower().endswith(".json"):
        data = read_json(path, {}) or {}
        result["title"] = str(data.get("title") or data.get("标题") or "")
        tags = data.get("tags") or data.get("标签") or []
        result["tags"] = [str(t).strip() for t in tags] if isinstance(tags, list) else [str(tags)]
        result["description"] = str(data.get("description") or data.get("描述") or "")
        return result

    body_lines = []
    for line in read_text(path).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            key, sep, value = stripped.partition("：")
        key_clean = key.strip().lower()
        if sep and key_clean in _TAG_KEYS:
            result["tags"] = [t.strip().lstrip("#") for t in value.replace("，", ",").split(",") if t.strip()]
        elif sep and key_clean in _TITLE_KEYS:
            result["title"] = value.strip()
        elif sep and key_clean in _DESC_KEYS:
            body_lines.append(value.strip())
        else:
            body_lines.append(stripped)
    result["description"] = " ".join(body_lines).strip()
    return result


def filename_tags(path: str, limit: int = 6) -> list:
    stem = os.path.splitext(os.path.basename(path))[0]
    for sep in ["-", "_", "。", " ", "\u3000"]:
        stem = stem.replace(sep, "\x00")
    tags = []
    for part in stem.split("\x00"):
        part = part.strip()
        if len(part) < 2:
            continue
        if part.isdigit():
            continue
        if part.lower() in {"mp4", "mov", "final", "export", "vlog", "footage"}:
            continue
        tags.append(part)
    return tags[:limit]


def build_elements(tags: list, title: str, description: str) -> list:
    elements = lexicon.elements_from_tags(tags, description=description, title=title)
    if not elements:
        fallback = (title or description or "").strip()[:12]
        if fallback:
            elements = [Element(type="topic", value=fallback, weight=0.5, confidence=0.4, evidence="文件名兜底")]
    return elements


def fingerprint(path: str) -> str:
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    return f"{int(stat.st_mtime)}-{stat.st_size}"


def build_material(file_path: str, materials_dir: str, keyframes_dir: str, *,
                   vision=None, extract_frames: bool = True,
                   frame_count: int = 3, frame_max_width: int = 720) -> Material:
    """把单个素材文件变成 `Material`：旁车说明 → 文件名标签 → 要素 → 探测 → 抽帧 → 可选视觉打标。

    输入：`file_path`（素材文件）、`materials_dir`（用于算 `Material.make_id` 的相对根）、
    `keyframes_dir`（关键帧落盘根目录，实际写到 `keyframes_dir/<material_id>/`）。
    可注入点：`vision`（`(frames, hint) -> dict | None`，None = 不打标）、
    `extract_frames`（False = 不抽帧）、`frame_count` / `frame_max_width`（抽帧数量与缩放宽度）。
    输出：`Material`（未入库；`indexed_at` 取当前时间，`fingerprint` = 文件 mtime+size）。
    依赖：ffmpeg / ffprobe 缺失时只跳过抽帧与探测（能力裁剪），不报错。
    """
    rel = os.path.relpath(file_path, materials_dir)
    material_id = Material.make_id(rel)
    fp = fingerprint(file_path)
    stat = os.stat(file_path)
    kind = media.kind_of(file_path)
    info = media.probe(file_path) if kind == "video" else _image_size(file_path)
    sidecar = parse_sidecar(sidecar_path(file_path) or "")
    tags = list(sidecar["tags"]) or filename_tags(file_path)
    title = sidecar["title"] or os.path.splitext(os.path.basename(file_path))[0]
    description = sidecar["description"]
    source = "sidecar" if (sidecar["description"] or sidecar["tags"]) else "filename"

    frames: list = []
    if extract_frames:
        frames = media.extract_keyframes(
            file_path,
            os.path.join(keyframes_dir, material_id),
            count=frame_count,
            max_width=frame_max_width,
        )

    if vision is not None and frames and not sidecar["description"]:
        try:
            described = vision(frames, title)
        except Exception:
            described = None
        if described:
            description = described.get("description") or description
            tags = _merge_tags(tags, described.get("tags") or [])
            if described.get("title"):
                title = described["title"]
            source = "vision"

    return Material(
        id=material_id,
        path=file_path,
        type=kind if kind != "other" else "video",
        title=title,
        description=description,
        tags=tags,
        elements=build_elements(tags, title, description),
        duration_s=float(info.get("duration_s") or 0.0),
        width=int(info.get("width") or 0),
        height=int(info.get("height") or 0),
        has_audio=bool(info.get("has_audio")),
        size_bytes=int(stat.st_size),
        mtime=float(stat.st_mtime),
        source=source,
        keyframes=frames,
        indexed_at=now_iso(),
        fingerprint=fp,
    )


def _merge_tags(existing: list, extra: list, limit: int = 14) -> list:
    out = []
    for tag in list(existing) + list(extra):
        tag = str(tag).strip().lstrip("#")
        if tag and tag not in out:
            out.append(tag)
    return out[:limit]


def _image_size(path: str) -> dict:
    info = media.probe(path)
    if info.get("width"):
        return info
    try:
        from struct import unpack

        with open(path, "rb") as fh:
            head = fh.read(26)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            width, height = unpack(">II", head[16:24])
            return {"duration_s": 0.0, "width": width, "height": height, "has_audio": False}
        if head[:2] == b"\xff\xd8":
            return _jpeg_size(path)
    except Exception:
        pass
    return {"duration_s": 0.0, "width": 0, "height": 0, "has_audio": False}


def _jpeg_size(path: str) -> dict:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.read(2)
        while fh.tell() < size:
            marker = fh.read(2)
            if len(marker) < 2 or marker[0] != 0xFF:
                break
            code = marker[1]
            if code in (0xD8, 0xD9) or 0xD0 <= code <= 0xD7:
                continue
            length_bytes = fh.read(2)
            if len(length_bytes) < 2:
                break
            length = int.from_bytes(length_bytes, "big")
            if code in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                data = fh.read(5)
                height = int.from_bytes(data[1:3], "big")
                width = int.from_bytes(data[3:5], "big")
                return {"duration_s": 0.0, "width": width, "height": height, "has_audio": False}
            fh.seek(length - 2, os.SEEK_CUR)
    return {"duration_s": 0.0, "width": 0, "height": 0, "has_audio": False}
