"""Walking Skeleton 脚本的纯函数单测（S3.6）：按路径加载脚本，不连库、不联网、不起进程。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "smoke_skeleton.py"
PACK_DIR = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "demo_pack"


@pytest.fixture(scope="module")
def sk():
    """按路径加载冒烟脚本（有 `__main__` 守护，导入无副作用）。"""
    spec = importlib.util.spec_from_file_location("smoke_skeleton_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate(**overrides) -> dict:
    base = {
        "material_id": "m-1",
        "score": 0.71,
        "hits": [{"element_type": "topic", "clue_value": "羽毛球", "hit_value": "羽毛球"}],
        "reasons": ["命中主题：羽毛球 ↔ 素材的「羽毛球」"],
        "material": {"title": "球场热身", "path": "运动/球场-挥拍.mp4"},
    }
    base.update(overrides)
    return base


def detail(candidates: list[dict]) -> dict:
    return {"hotspots": [{"candidates": candidates}]}


class TestLoadHotspot:
    def test_default_case_is_readable(self, sk) -> None:
        hotspot = sk.load_hotspot()
        assert "羽毛球" in hotspot

    def test_unknown_case_raises(self, sk) -> None:
        with pytest.raises(sk.SkeletonError, match="用例不存在"):
            sk.load_hotspot("case-99")


class TestSeedMaterials:
    def test_seeds_nested_files_and_sidecars(self, sk, tmp_path: Path) -> None:
        pack = tmp_path / "pack"
        (pack / "运动").mkdir(parents=True)
        (pack / "运动" / "球场-挥拍.mp4").write_bytes(b"\x00" * 8)
        (pack / "运动" / "球场-挥拍.mp4.txt").write_text("标题: 球场热身", encoding="utf-8")
        (pack / ".hidden.mp4").write_bytes(b"\x00" * 8)
        target = tmp_path / "materials"

        copied = sk.seed_materials(str(target), pack_dir=pack)

        assert copied == 2                      # 旁车一起复制，隐藏文件不复制
        assert (target / "运动" / "球场-挥拍.mp4").is_file()
        assert (target / "运动" / "球场-挥拍.mp4.txt").is_file()
        assert not (target / ".hidden.mp4").exists()

    def test_second_call_is_a_noop(self, sk, tmp_path: Path) -> None:
        pack = tmp_path / "pack"
        pack.mkdir()
        (pack / "a.jpg").write_bytes(b"\x00")
        target = tmp_path / "materials"
        assert sk.seed_materials(str(target), pack_dir=pack) == 1
        assert sk.seed_materials(str(target), pack_dir=pack) == 0

    def test_non_empty_library_is_untouched(self, sk, tmp_path: Path) -> None:
        """用户自己的素材库绝不能被种包污染。"""
        pack = tmp_path / "pack"
        pack.mkdir()
        (pack / "a.jpg").write_bytes(b"\x00")
        target = tmp_path / "materials"
        target.mkdir()
        (target / "我的素材.jpg").write_bytes(b"\x01")

        assert sk.seed_materials(str(target), pack_dir=pack) == 0
        assert not (target / "a.jpg").exists()

    def test_gitkeep_does_not_count_as_material(self, sk, tmp_path: Path) -> None:
        pack = tmp_path / "pack"
        pack.mkdir()
        (pack / "a.jpg").write_bytes(b"\x00")
        target = tmp_path / "materials"
        target.mkdir()
        (target / ".gitkeep").write_text("", encoding="utf-8")

        assert sk.seed_materials(str(target), pack_dir=pack) == 1

    def test_frozen_demo_pack_is_usable(self, sk, tmp_path: Path) -> None:
        copied = sk.seed_materials(str(tmp_path / "materials"), pack_dir=PACK_DIR)
        assert copied >= 20                     # 19 条素材 + 同名旁车
        assert (tmp_path / "materials" / "运动" / "球场-挥拍.mp4.txt").is_file()


class TestVerifyRunDetail:
    def test_returns_top_candidate(self, sk) -> None:
        top = sk.verify_run_detail(detail([candidate(), candidate(material_id="m-2")]))
        assert top["material_id"] == "m-1"

    def test_no_hotspots_raises(self, sk) -> None:
        with pytest.raises(sk.SkeletonError, match="没有热点"):
            sk.verify_run_detail({"hotspots": []})

    def test_no_candidates_explains_materials(self, sk) -> None:
        with pytest.raises(sk.SkeletonError, match="没有候选素材"):
            sk.verify_run_detail(detail([]))

    def test_candidate_without_hits_raises(self, sk) -> None:
        with pytest.raises(sk.SkeletonError, match="缺少命中要素"):
            sk.verify_run_detail(detail([candidate(hits=[])]))

    def test_candidate_without_reasons_raises(self, sk) -> None:
        with pytest.raises(sk.SkeletonError, match="缺少理由"):
            sk.verify_run_detail(detail([candidate(reasons=[])]))


class TestVerifyReport:
    def test_complete_html_passes(self, sk) -> None:
        body = "<html><body>某明星打羽毛球" + "报告正文" * 200 + "</body></html>"
        sk.verify_report(body, fmt="html", hotspot="某明星打羽毛球")

    def test_incomplete_html_raises(self, sk) -> None:
        with pytest.raises(sk.SkeletonError, match="不完整"):
            sk.verify_report("<html><body>" + "x" * 800, fmt="html")

    def test_too_short_raises(self, sk) -> None:
        with pytest.raises(sk.SkeletonError, match="过短"):
            sk.verify_report("<html></html>", fmt="html")

    def test_missing_hotspot_raises(self, sk) -> None:
        body = "<html><body>" + "报告正文" * 200 + "</body></html>"
        with pytest.raises(sk.SkeletonError, match="热点原文"):
            sk.verify_report(body, fmt="html", hotspot="某明星打羽毛球")

    def test_escaped_hotspot_is_accepted(self, sk) -> None:
        body = "<html><body>" + "报告正文" * 200 + "A&amp;B</body></html>"
        sk.verify_report(body, fmt="html", hotspot="A&B")

    def test_markdown_needs_no_html_tags(self, sk) -> None:
        sk.verify_report("# 报告\n" + "正文" * 300, fmt="md")


class TestNaming:
    def test_report_path_uses_date(self, sk, tmp_path: Path) -> None:
        path = sk.report_path("html", date="2026-10-03", reports_dir=tmp_path)
        assert path.name == "skeleton-2026-10-03.html"

    def test_worker_command_is_windows_safe(self, sk) -> None:
        command = sk.worker_command()
        assert "xhs_agent.tasks.worker:app" in command
        assert "--pool=solo" in command

    def test_api_command_carries_the_port(self, sk) -> None:
        command = sk.api_command(8123)
        assert "xhs_agent.api.main:app" in command
        assert "8123" in command


class TestParseArgs:
    def test_defaults(self, sk) -> None:
        args = sk.parse_args([])
        assert args.case == "case-01"
        assert args.api_url == ""
        assert args.no_seed is False
        assert args.keep_alive is False
        assert args.timeout == 300

    def test_overrides(self, sk) -> None:
        args = sk.parse_args(["--case", "case-02", "--api-url", "http://127.0.0.1:8000",
                             "--no-seed", "--keep-alive", "--timeout", "60"])
        assert (args.case, args.api_url, args.no_seed, args.keep_alive,
                args.timeout) == ("case-02", "http://127.0.0.1:8000", True, True, 60)
