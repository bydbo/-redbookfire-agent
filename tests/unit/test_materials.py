"""素材扫描与索引单测：数据全部落在 tmp_path，且不依赖机器上有没有 ffmpeg。"""

from __future__ import annotations

import json
import os
import pathlib

from xhs_agent.tools import materials


class TestScanMaterials:
    def test_finds_media_files_only(self, materials_dir, no_ffmpeg):
        found = {os.path.basename(p) for p in materials.scan_materials(str(materials_dir))}
        assert found == {"球场-挥拍.mp4", "宠物猫.png"}

    def test_ignores_hidden_files(self, materials_dir, no_ffmpeg):
        found = materials.scan_materials(str(materials_dir))
        assert not any(os.path.basename(p).startswith(".") for p in found)

    def test_non_recursive_skips_subdirectories(self, materials_dir, no_ffmpeg):
        sub = materials_dir / "sub"
        sub.mkdir()
        (sub / "clip.mp4").write_bytes(b"\x00")
        recursive = {os.path.basename(p) for p in materials.scan_materials(str(materials_dir))}
        flat = {os.path.basename(p)
                for p in materials.scan_materials(str(materials_dir), recursive=False)}
        assert "clip.mp4" in recursive
        assert "clip.mp4" not in flat

    def test_missing_directory_returns_empty(self, tmp_path):
        assert materials.scan_materials(str(tmp_path / "nope")) == []


class TestSidecar:
    def test_finds_txt_sidecar(self, materials_dir):
        assert materials.sidecar_path(str(materials_dir / "球场-挥拍.mp4")).endswith(".mp4.txt")

    def test_returns_none_without_sidecar(self, materials_dir):
        assert materials.sidecar_path(str(materials_dir / "宠物猫.png")) is None

    def test_parses_key_value_sidecar(self, materials_dir):
        parsed = materials.parse_sidecar(str(materials_dir / "球场-挥拍.mp4.txt"))
        assert parsed["title"] == "球场热身"
        assert parsed["tags"] == ["羽毛球", "挥拍"]

    def test_parses_plain_text_as_description(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text("清晨的球场，光很好", encoding="utf-8")
        assert materials.parse_sidecar(str(path))["description"] == "清晨的球场，光很好"

    def test_parses_json_sidecar(self, tmp_path):
        path = tmp_path / "a.json"
        path.write_text(json.dumps({"标题": "测试", "标签": ["羽毛球"], "描述": "说明"},
                                   ensure_ascii=False), encoding="utf-8")
        parsed = materials.parse_sidecar(str(path))
        assert (parsed["title"], parsed["tags"], parsed["description"]) == \
            ("测试", ["羽毛球"], "说明")

    def test_missing_file_returns_empty_shape(self, tmp_path):
        assert materials.parse_sidecar(str(tmp_path / "nope.txt")) == \
            {"title": "", "tags": [], "description": ""}


class TestTagsAndElements:
    def test_filename_tags_split_and_drop_generic_words(self):
        assert materials.filename_tags("球场-挥拍_final.mp4") == ["球场", "挥拍"]

    def test_filename_tags_drop_digits(self):
        assert materials.filename_tags("2026-vlog-01.mp4") == []

    def test_build_elements_uses_lexicon(self):
        values = {e.value for e in materials.build_elements(["羽毛球"], "球场", "")}
        assert {"羽毛球", "球场"} <= values

    def test_build_elements_falls_back_to_title(self):
        elements = materials.build_elements([], title="完全陌生的标题", description="")
        assert [(e.type, e.evidence) for e in elements] == [("topic", "文件名兜底")]

    def test_build_elements_without_any_hint_returns_empty(self):
        assert materials.build_elements([], title="", description="") == []


class TestFingerprint:
    def test_follows_file_content(self, tmp_path):
        path = tmp_path / "a.mp4"
        path.write_bytes(b"1")
        first = materials.fingerprint(str(path))
        path.write_bytes(b"12")
        assert materials.fingerprint(str(path)) != first

    def test_missing_file_returns_empty(self, tmp_path):
        assert materials.fingerprint(str(tmp_path / "nope.mp4")) == ""


class TestBuildMaterial:
    """单文件构造器：离线、临时目录，可独立测试。"""

    def test_sidecar_wins_over_filename(self, materials_dir, tmp_path, no_ffmpeg):
        material = materials.build_material(
            str(materials_dir / "球场-挥拍.mp4"),
            str(materials_dir),
            str(tmp_path / "kf"),
            extract_frames=False,
        )
        assert (material.source, material.title) == ("sidecar", "球场热身")
        assert material.tags == ["羽毛球", "挥拍"]
        assert material.fingerprint == materials.fingerprint(str(materials_dir / "球场-挥拍.mp4"))
        assert material.id == materials.Material.make_id("球场-挥拍.mp4")

    def test_falls_back_to_filename_without_sidecar(self, tmp_path, make_png, no_ffmpeg):
        root = tmp_path / "m"
        root.mkdir()
        (root / "晨跑-公园.png").write_bytes(make_png(720, 1280))
        material = materials.build_material(str(root / "晨跑-公园.png"), str(root),
                                            str(tmp_path / "kf"), extract_frames=False)
        assert material.source == "filename"
        assert material.title == "晨跑-公园"
        assert material.tags == ["晨跑", "公园"]
        assert (material.type, material.width, material.height) == ("image", 720, 1280)

    def test_frame_params_are_forwarded(self, materials_dir, tmp_path, monkeypatch):
        seen = {}

        def fake_extract(_path, out_dir, count=3, max_width=720, prefix="kf"):
            seen["count"] = count
            seen["max_width"] = max_width
            pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)
            target = pathlib.Path(out_dir) / "kf_0.jpg"
            target.write_bytes(b"\x00")
            return [str(target)]

        monkeypatch.setattr(materials.media, "extract_keyframes", fake_extract)
        material = materials.build_material(str(materials_dir / "球场-挥拍.mp4"),
                                            str(materials_dir), str(tmp_path / "kf"),
                                            frame_count=7, frame_max_width=480)
        assert seen == {"count": 7, "max_width": 480}
        assert material.keyframes == [str(tmp_path / "kf" / material.id / "kf_0.jpg")]

    def test_extract_frames_false_skips_ffmpeg(self, materials_dir, tmp_path, monkeypatch):
        def boom(*_args, **_kwargs):
            raise AssertionError("extract_frames=False 时不应调用抽帧")

        monkeypatch.setattr(materials.media, "extract_keyframes", boom)
        material = materials.build_material(str(materials_dir / "球场-挥拍.mp4"),
                                            str(materials_dir), str(tmp_path / "kf"),
                                            extract_frames=False)
        assert material.keyframes == []

    def test_vision_result_overrides_description_and_source(self, materials_dir, tmp_path,
                                                            monkeypatch):
        def fake_extract(_path, out_dir, count=3, max_width=720, prefix="kf"):
            pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)
            target = pathlib.Path(out_dir) / "kf_0.jpg"
            target.write_bytes(b"\x00")
            return [str(target)]

        monkeypatch.setattr(materials.media, "extract_keyframes", fake_extract)
        material = materials.build_material(
            str(materials_dir / "宠物猫.png"), str(materials_dir), str(tmp_path / "kf"),
            vision=lambda _frames, _hint: {"title": "猫", "tags": ["猫"], "description": "打盹"},
        )
        assert (material.source, material.title, material.description) == ("vision", "猫", "打盹")

    def test_vision_is_not_called_without_frames(self, materials_dir, tmp_path):
        """没有关键帧（抽帧被跳过）时，即使传了 vision 也不该调用它。"""

        def boom(*_args, **_kwargs):
            raise AssertionError("没有关键帧时不应调用视觉打标")

        material = materials.build_material(str(materials_dir / "宠物猫.png"),
                                            str(materials_dir), str(tmp_path / "kf"),
                                            vision=boom, extract_frames=False)
        assert material.keyframes == []
        assert material.source != "vision"
