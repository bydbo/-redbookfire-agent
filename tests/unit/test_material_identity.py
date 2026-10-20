"""身份纯逻辑：跨平台路径、确定性计划、证据选择和安全拒绝。"""

from __future__ import annotations

import copy

import pytest

from xhs_agent.services.material_identity_migration import TABLES, build_plan
from xhs_agent.tools.material_paths import (
    historical_key,
    material_key,
    resolve_material,
    validate_key,
)


@pytest.mark.parametrize("key", ["", " ", "   ", ".", "..", "../a.mp4", "a/../x.mp4", "/a.mp4",
                                "C:/a.mp4", "C:a.mp4", "\\\\server\\a.mp4", "a\\b.mp4",
                                "a//b.mp4", "a/./b.mp4", "a.mp4/", "a\x00.mp4",
                                " a.mp4", "a.mp4 ", "a\x7f.mp4"])
def test_invalid_material_keys(key):
    with pytest.raises(ValueError):
        validate_key(key)


@pytest.mark.parametrize(("path", "root", "flavor"), [
    ("D:\\旧项目\\data\\materials\\运动\\a.mp4", "D:\\旧项目\\data\\materials", "windows"),
    ("/app/data/materials/运动/a.mp4", "/app/data/materials", "posix"),
    ("\\\\server\\share\\media\\运动\\a.mp4", "\\\\server\\share\\media", "windows"),
])
def test_foreign_roots_are_explicit_and_platform_independent(path, root, flavor):
    assert historical_key(path, [{"root": root, "flavor": flavor}]) == "运动/a.mp4"


def test_unknown_and_overlapping_roots_do_not_guess():
    with pytest.raises(ValueError):
        historical_key("/backup/app/data/materials/a.mp4", [{"root": "/app/data/materials", "flavor": "posix"}])
    with pytest.raises(ValueError):
        historical_key("/app/data/materials/a.mp4", [
            {"root": "/app", "flavor": "posix"},
            {"root": "/app/data/materials", "flavor": "posix"}])
    assert historical_key("data/materials/a.mp4", [
        {"root": "data/materials", "flavor": "relative"}], relative_basis="mapped") == "a.mp4"


@pytest.mark.parametrize(("path", "mapping"), [
    # 相对根不能声明成 windows / posix
    ("/app/media/a.mp4", {"root": "app/media", "flavor": "posix"}),
    # 根自身含穿越段
    ("/app/media/a.mp4", {"root": "/app/../media", "flavor": "posix"}),
    ("D:\\app\\media\\a.mp4", {"root": "D:\\app\\..\\media", "flavor": "windows"}),
    # 相对来源的根必须是合法键
    ("app/media/a.mp4", {"root": "../media", "flavor": "relative"}),
    # UNC 根不能混作 POSIX
    ("//server/share/a.mp4", {"root": "//server/share", "flavor": "posix"}),
])
def test_invalid_root_mappings_are_rejected(path, mapping):
    with pytest.raises(ValueError):
        historical_key(path, [mapping], relative_basis="mapped")


def test_overlapping_mappings_must_agree():
    # 同一目录用尾斜杠重复声明 → 得到同一个键，允许
    assert historical_key("/app/media/a.mp4", [
        {"root": "/app/media", "flavor": "posix"},
        {"root": "/app/media/", "flavor": "posix"},
    ]) == "a.mp4"


def test_root_outside_project_and_missing_media(tmp_path):
    root = tmp_path / "外部 目录"
    root.mkdir()
    path = root / "运动" / "a.mp4"
    assert material_key(str(path), str(root)) == "运动/a.mp4"
    assert resolve_material(str(root), "运动/a.mp4") == str(path.resolve())


def _data():
    data = {table: [] for table in TABLES}
    data["materials"] = [
        {"id": "00000000-0000-0000-0000-000000000001", "path": "/app/media/a.mp4",
         "source": "filename", "indexed_at": "2026-10-19T00:00:00+00:00", "title": "新自动"},
        {"id": "00000000-0000-0000-0000-000000000002", "path": "a.mp4",
         "source": "sidecar", "indexed_at": "2026-10-18T00:00:00+00:00", "title": "人工"},
    ]
    data["run_matches"] = [
        {"run_hotspot_id": "hotspot", "material_id": data["materials"][0]["id"],
         "score": "0.9000", "rank": 4, "reasons": ["完整保留理由"]},
        {"run_hotspot_id": "hotspot", "material_id": data["materials"][1]["id"],
         "score": "0.8000", "rank": 1, "reasons": ["另一理由"]},
    ]
    return data


def plan(data):
    return build_plan(data, identity={"name": "test"}, mappings=[
        {"root": "/app/media", "flavor": "posix"}], relative_basis="material",
        revision="0012", code_version="test")


def test_plan_preserves_human_metadata_and_winner_rank_without_mutating_inputs():
    data = _data()
    original = copy.deepcopy(data)
    result = plan(data)
    assert result == plan(copy.deepcopy(data))
    assert data == original
    assert result["groups"][0]["canonical_id"] == data["materials"][1]["id"]
    assert result["candidate_decisions"][0]["rank"] == 4
    assert result["candidate_decisions"][0]["score"] == "0.9000"
    assert result["counts"]["matches_after"] == 1
    assert result["issues"] == []


def test_case_conflicts_are_blocking():
    data = _data()
    data["materials"][0]["path"] = "A.mp4"
    assert plan(data)["issues"]


def test_unmapped_is_blocking_and_does_not_remove_original():
    data = _data()
    data["materials"][0]["path"] = "/unknown/a.mp4"
    result = plan(data)
    assert result["issues"]
    assert len(result["source_materials"]) == 2


def test_archive_manifest_is_anchored_to_plan_source():
    data = _data()
    result = plan(data)
    manifest = result["archive_manifest"]
    assert len(manifest) == 4          # 2 素材 + 2 条受影响候选（run_hotspots 为空）
    assert {item["entity_type"] for item in manifest} == {"materials", "run_matches"}
    assert all(item["digest"] for item in manifest)
    materials = {item["original_key"] for item in manifest
                 if item["entity_type"] == "materials"}
    assert materials == {row["id"] for row in data["materials"]}


def test_existing_alias_repoints_and_is_archived():
    data = _data()
    old = "00000000-0000-0000-0000-0000000000aa"
    data["material_id_aliases"] = [
        {"old_id": old, "canonical_id": data["materials"][0]["id"], "batch_id": "prior-batch"},
    ]
    result = plan(data)
    assert result["issues"] == []
    assert result["aliases"][old] == data["materials"][1]["id"]
    assert result["alias_updates"] == [{
        "old_id": old, "from": data["materials"][0]["id"],
        "to": data["materials"][1]["id"],
    }]
    assert any(item["entity_type"] == "material_id_aliases" for item in result["archive_manifest"])


def test_alias_cycle_is_blocking():
    data = _data()
    old = "00000000-0000-0000-0000-0000000000aa"
    data["material_id_aliases"] = [
        {"old_id": old, "canonical_id": data["materials"][0]["id"], "batch_id": "prior-batch"},
        {"old_id": data["materials"][0]["id"], "canonical_id": old, "batch_id": "prior-batch"},
    ]
    assert any("链或环" in item["reason"] for item in plan(data)["issues"])


def test_archive_values_are_losslessly_decodable():
    from decimal import Decimal

    from xhs_agent.services.material_identity_migration import decode_record

    decoded = decode_record("materials", {
        "id": "m1", "embedding": "[0.5, 1.25, -2.0]", "duration_s": "12.50",
    })
    assert decoded["embedding"] == [0.5, 1.25, -2.0]
    assert decoded["duration_s"] == Decimal("12.50")
    assert decode_record("run_matches", {"score": "0.9000"})["score"] == Decimal("0.9000")
    with pytest.raises(ValueError):
        decode_record("materials", {"embedding": "not-json"})


def test_implementation_version_binds_the_worktree():
    """S6.10c：正式计划要绑定被审查实现，而不只是一个可能很旧的 HEAD。"""
    import importlib.util
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "normalize_material_identity.py"
    spec = importlib.util.spec_from_file_location("normalize_identity_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    version = module.implementation_version()
    head, _, worktree = version.partition("+worktree:")
    assert len(head) >= 7 and worktree
    assert module.implementation_version() == version      # 同一工作区稳定
