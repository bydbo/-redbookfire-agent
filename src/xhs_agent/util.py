"""通用小工具：文本归一化、JSON 抽取、文件读写、相似度。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime
from typing import Any

CJK_RANGE = r"\u4e00-\u9fff"
_PUNCT_RE = re.compile(r"[\s\u3000\-_/\\|,，.。、;；:：!！?？~～^&*()（）\[\]【】{}<>《》\"'“”‘’+#@$%]+")
_SLUG_RE = re.compile(rf"[^{CJK_RANGE}a-zA-Z0-9]+")


def now_iso() -> str:
    """本地时区的 ISO 时间戳，精确到秒。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 24, fallback: str = "run") -> str:
    """把任意文本压成适合做目录名/文件名的短串，保留中文和英文数字。"""
    text = unicodedata.normalize("NFKC", str(text)).strip()
    slug = _SLUG_RE.sub("-", text).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        return fallback
    return slug[:max_len].strip("-") or fallback


def normalize_text(text: Any) -> str:
    """归一化文本用于比较：全角转半角、转小写、去空白与标点。"""
    if text is None:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).lower()
    return _PUNCT_RE.sub("", text)


def char_ngrams(text: str, n: int = 2) -> set:
    """中文字符 n-gram 集合，用于短文本相似度。"""
    text = normalize_text(text)
    if not text:
        return set()
    if len(text) <= n:
        return {text}
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def text_similarity(a: str, b: str) -> float:
    """0~1 的短文本相似度：完全相等 > 包含 > 字符 bigram 重合。"""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        ratio = min(len(na), len(nb)) / max(len(na), len(nb))
        return 0.72 + 0.23 * ratio
    return jaccard(char_ngrams(na), char_ngrams(nb))


def extract_json(text: str) -> Any:
    """从模型输出里抠出 JSON，容忍代码块围栏和前后废话。"""
    if text is None:
        raise ValueError("模型返回为空，无法解析 JSON")
    raw = str(text).strip()
    candidates = re.findall(r"```(?:json)?\s*(.+?)```", raw, flags=re.S | re.I) + [raw]
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            candidates.append(raw[start:end + 1])
    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"无法从模型输出中解析 JSON：{raw[:300]}")


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def write_text(path: str, text: str) -> str:
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def read_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: str, obj: Any) -> str:
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    return path


def append_jsonl(path: str, obj: Any) -> str:
    _ensure_parent(path)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return path


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def short_hash(text: str, length: int = 10) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:length]


def clip01(value: Any, default: float = 0.5) -> float:
    """把任意输入压到 0~1。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:
        return default
    return max(0.0, min(1.0, num))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def human_size(num_bytes: float) -> str:
    units = ["B", "KB", "MB", "GB"]
    size = float(num_bytes or 0)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{int(size)}B" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def truncate(text: str, length: int = 120, suffix: str = "…") -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= length else text[:length] + suffix