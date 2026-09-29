"""素材库：扫描、打标签、建索引。

索引约定（非常重要，决定了匹配质量的上下限）：
1. 同目录下 `素材名.mp4.txt` / `素材名.md` 会被当成人工说明，优先级最高；
2. 文件名里的 `-` `_` 分隔词会被当作标签；
3. 开启视觉模型后，会自动抽帧并让模型看图补描述；
4. 索引按 路径+修改时间+大小 做增量，改动的素材才会重新识别。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..schemas import Element, Material
from ..util import now_iso, read_json, read_text, short_hash, write_json
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


@dataclass
class IndexResult:
    materials: list
    added: int = 0
    updated: int = 0
    reused: int = 0
    index_path: str = ""

    def summary(self) -> str:
        return (f"素材 {len(self.materials)} 条：新增 {self.added}、更新 {self.updated}、"
                f"复用缓存 {self.reused}")


def index_path_for(index_dir: str, materials_dir: str) -> str:
    return os.path.join(index_dir, f"materials-{short_hash(os.path.abspath(materials_dir), 8)}.json")


def load_index(index_dir: str, materials_dir: str) -> tuple:
    path = index_path_for(index_dir, materials_dir)
    data = read_json(path, None)
    if not isinstance(data, dict):
        return [], path
    items = [Material.from_dict(item) for item in (data.get("materials") or [])]
    return items, path


def save_index(index_dir: str, materials_dir: str, materials: list) -> str:
    path = index_path_for(index_dir, materials_dir)
    write_json(path, {
        "materials_dir": os.path.abspath(materials_dir),
        "updated_at": now_iso(),
        "count": len(materials),
        "materials": [m.to_dict() for m in materials],
    })
    return path


def build_index(materials_dir: str, index_dir: str, vision=None,
                keyframes_dir: str | None = None, force: bool = False,
                max_vision_items: int = 50) -> IndexResult:
    """扫描并建立素材索引。vision 为可调用对象时用于看图补描述。"""
    files = scan_materials(materials_dir)
    keyframes_dir = keyframes_dir or os.path.join(index_dir, "keyframes")
    cached, path = load_index(index_dir, materials_dir)
    cache_by_path = {} if force else {os.path.abspath(m.path): m for m in cached}

    result = IndexResult(materials=[], index_path=path)
    vision_used = 0

    for file_path in files:
        rel = os.path.relpath(file_path, materials_dir)
        material_id = Material.make_id(rel)
        fp = fingerprint(file_path)
        cached_item = cache_by_path.get(os.path.abspath(file_path))
        if cached_item and cached_item.fingerprint == fp and fp:
            result.materials.append(cached_item)
            result.reused += 1
            continue

        stat = os.stat(file_path)
        kind = media.kind_of(file_path)
        info = media.probe(file_path) if kind == "video" else _image_size(file_path)
        sidecar = parse_sidecar(sidecar_path(file_path) or "")
        tags = list(sidecar["tags"]) or filename_tags(file_path)
        title = sidecar["title"] or os.path.splitext(os.path.basename(file_path))[0]
        description = sidecar["description"]
        source = "sidecar" if (sidecar["description"] or sidecar["tags"]) else "filename"

        frames = media.extract_keyframes(
            file_path,
            os.path.join(keyframes_dir, material_id),
            count=3,
            max_width=720,
        )

        if vision is not None and frames and vision_used < max_vision_items and not sidecar["description"]:
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
                vision_used += 1

        material = Material(
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
        result.materials.append(material)
        if cached_item:
            result.updated += 1
        else:
            result.added += 1

    save_index(index_dir, materials_dir, result.materials)
    return result


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