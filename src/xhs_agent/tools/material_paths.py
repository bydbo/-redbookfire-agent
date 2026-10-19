"""素材键与本机位置转换；不访问数据库，不猜测历史根。

validate_key 是纯词法校验；material_key / resolve_material 检查文件系统边界及链接，
允许文件尚不存在，但拒绝素材根以下的链接 / junction。错误统一为 ValueError。
"""

from __future__ import annotations

import ntpath
import os
from pathlib import Path


def validate_key(value: str) -> str:
    """返回未经大小写 / Unicode 改写的素材根相对键。

    拒绝：空 / 纯空白、盘符、UNC、绝对根、反斜杠、空段、`.`、`..`、首尾空白的段、
    控制字符（含 DEL）。不做大小写折叠——大小写歧义由上层计划 / 启动检查报告。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("素材路径必须是无穿越段的素材根相对正斜杠路径")
    parts = value.split("/")
    if (
        value.startswith("/")
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} or part != part.strip() for part in parts)
    ):
        raise ValueError("素材路径必须是无穿越段的素材根相对正斜杠路径")
    return value


def is_link(path: str | Path) -> bool:
    return os.path.islink(path) or bool(getattr(os.path, "isjunction", lambda _: False)(path))


def resolve_material(root: str, key: str) -> str:
    """将规范键转换为本机绝对路径；根以下的链接一律拒绝。"""
    validate_key(key)
    base = Path(root).resolve()
    target = base
    for part in key.split("/"):
        target = target / part
        if is_link(target):
            raise ValueError("素材根以下不允许符号链接或 junction")
    resolved = target.resolve()
    if not resolved.is_relative_to(base) or resolved == base:
        raise ValueError("素材路径越出了允许目录")
    return str(resolved)


def material_key(path: str, root: str) -> str:
    """从本机扫描路径生成规范键，检查前不 resolve 以免掩盖链接。"""
    base = Path(os.path.abspath(root))
    target = Path(os.path.abspath(path))
    try:
        key = target.relative_to(base).as_posix()
    except ValueError as exc:
        raise ValueError("素材不在配置素材根目录下") from exc
    validate_key(key)
    resolve_material(root, key)
    return key


def _validate_mapping(flavor: str, root: str) -> str:
    """校验并返回规范化根；非法声明直接失败，不进入匹配。"""
    if flavor not in {"windows", "posix", "relative"} or not root:
        raise ValueError("根映射必须声明 windows / posix / relative 与非空 root")
    if flavor == "windows":
        if not ntpath.isabs(root) or not ntpath.splitdrive(root)[0]:
            raise ValueError("Windows 根映射必须是带盘符或 UNC 的绝对目录")
        normalized = root.replace("\\", "/").rstrip("/")
        if ".." in normalized.split("/"):
            raise ValueError("根映射不能包含 .. 段")
        return normalized
    if flavor == "posix":
        if not root.startswith("/") or root.startswith("//"):
            raise ValueError("POSIX 根映射必须是单个 / 开头的绝对目录")
        normalized = root.rstrip("/")
        if ".." in normalized.split("/"):
            raise ValueError("根映射不能包含 .. 段")
        return normalized
    normalized = validate_key(root.rstrip("/"))
    return normalized


def historical_key(path: str, mappings: list[dict[str, str]], *,
                   relative_basis: str = "material") -> str:
    """显式 Windows / POSIX / 根映射；relative_basis 为 material 或 mapped。

    映射按完整目录段匹配（根名大小写不敏感、后缀保留大小写）；重叠映射只有得到同一键时
    才接受。映射本身非法（相对根、`..`、UNC 混用 POSIX）直接失败，不静默跳过。
    """
    windows_absolute = bool(ntpath.splitdrive(path)[0]) or path.startswith("\\")
    posix_absolute = path.startswith("/")
    results: set[str] = set()
    for mapping in mappings:
        flavor = str(mapping.get("flavor") or "")
        root = _validate_mapping(flavor, str(mapping.get("root") or ""))
        if flavor == "windows":
            if not windows_absolute:
                continue
            candidate = path.replace("\\", "/")
            matched = candidate[:len(root)].casefold() == root.casefold()
        else:
            if flavor == "posix" and (windows_absolute or not posix_absolute):
                continue
            if flavor == "relative" and (windows_absolute or posix_absolute):
                continue
            candidate = path.replace("\\", "/")
            matched = candidate.startswith(root)
        if matched and candidate[len(root):].startswith("/"):
            results.add(validate_key(candidate[len(root) + 1:]))
    if not windows_absolute and not posix_absolute and relative_basis == "material":
        results.add(validate_key(path))
    if len(results) != 1:
        raise ValueError("历史素材根未知或映射歧义，请补充明确根映射")
    return results.pop()
