"""E8 CLI content identity is portable to a runtime image without Git."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "e8_snapshot.py"
SPEC = importlib.util.spec_from_file_location("e8_version_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _tree(root: Path, newline: str) -> None:
    for path in MODULE._VERSION_PATHS:
        target = root / path
        if "." not in target.name:
            target = target / "asset.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(("first" + newline + "second" + newline).encode())


def test_content_identity_without_git_is_portable_and_sensitive(tmp_path: Path) -> None:
    checkout, image = tmp_path / "checkout", tmp_path / "image"
    _tree(checkout, "\r\n")
    _tree(image, "\n")
    before = MODULE.implementation_version(checkout)
    assert before.startswith("source-sha256:")
    assert before == MODULE.implementation_version(image)
    cache = image / "src" / "__pycache__"
    cache.mkdir()
    (cache / "asset.pyc").write_bytes(b"runtime bytecode")
    assert before == MODULE.implementation_version(image)
    (image / "src" / "asset.py").write_text("changed source", encoding="utf-8")
    assert before != MODULE.implementation_version(image)


def test_content_identity_refuses_missing_sources(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="缺少实现版本"):
        MODULE.implementation_version(tmp_path)
