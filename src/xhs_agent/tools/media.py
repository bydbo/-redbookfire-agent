"""视频/图片探测与抽帧。

用途：判断文件是不是媒体、读出时长/分辨率/音轨、按时间点抽关键帧。
输入：文件路径（可选输出目录、帧数与缩放宽度）。
输出：`kind_of` → `video` / `image` / `other`；`probe` → 固定键的 dict
      （`duration_s` / `width` / `height` / `has_audio`）；`extract_keyframes` → 生成的文件路径列表。
依赖：ffmpeg / ffprobe。缺失或文件损坏时返回空结果（能力裁剪），不抛异常打断整条链路。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".wmv", ".ts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif"}
MEDIA_EXT = VIDEO_EXT | IMAGE_EXT


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def have_ffprobe() -> bool:
    return shutil.which("ffprobe") is not None


def kind_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    return "other"


def _run(cmd: list, timeout: int = 60) -> tuple:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def probe(path: str) -> dict:
    """读取时长/分辨率/是否有音轨。失败返回空字典，绝不抛异常打断整条链路。"""
    info = {"duration_s": 0.0, "width": 0, "height": 0, "has_audio": False}
    if not have_ffprobe() or not os.path.exists(path):
        return info
    code, out, _err = _run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ])
    if code != 0 or not out.strip():
        return info
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return info

    streams = data.get("streams") or []
    info["has_audio"] = any(s.get("codec_type") == "audio" for s in streams)
    for stream in streams:
        if stream.get("codec_type") == "video":
            info["width"] = int(stream.get("width") or 0)
            info["height"] = int(stream.get("height") or 0)
            break
    duration = (data.get("format") or {}).get("duration")
    try:
        info["duration_s"] = float(duration) if duration else 0.0
    except (TypeError, ValueError):
        info["duration_s"] = 0.0
    return info


def extract_keyframes(path: str, out_dir: str, count: int = 3, max_width: int = 720, prefix: str = "kf") -> list:
    """抽关键帧（图片则缩放一份）。返回生成的文件路径列表。"""
    if not have_ffmpeg() or not os.path.exists(path):
        return []
    os.makedirs(out_dir, exist_ok=True)
    kind = kind_of(path)
    outputs = []

    if kind == "image":
        target = os.path.join(out_dir, f"{prefix}_0.jpg")
        code, _out, _err = _run([
            "ffmpeg", "-y", "-v", "error", "-i", path,
            "-vf", f"scale='min({max_width},iw)':-2", "-frames:v", "1", target,
        ])
        return [target] if code == 0 and os.path.exists(target) else []

    if kind != "video":
        return []

    info = probe(path)
    duration = info.get("duration_s") or 0.0
    fractions = [0.12, 0.5, 0.85][:max(1, count)]
    if duration <= 0:
        timestamps = [0.5, 1.5, 2.5][:max(1, count)]
    else:
        timestamps = [max(0.1, duration * f) for f in fractions]

    for index, ts in enumerate(timestamps):
        target = os.path.join(out_dir, f"{prefix}_{index}.jpg")
        code, _out, _err = _run([
            "ffmpeg", "-y", "-v", "error", "-ss", f"{ts:.2f}", "-i", path,
            "-frames:v", "1", "-vf", f"scale='min({max_width},iw)':-2", "-q:v", "3", target,
        ])
        if code == 0 and os.path.exists(target):
            outputs.append(target)
    return outputs
