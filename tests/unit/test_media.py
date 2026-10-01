"""媒体探测单测：只验证「依赖缺失/文件有问题时行为稳定」，真抽帧留给集成测试。"""

from __future__ import annotations

import subprocess

import pytest

from xhs_agent.tools import media

EMPTY_INFO = {"duration_s": 0.0, "width": 0, "height": 0, "has_audio": False}


class TestKindOf:
    @pytest.mark.parametrize("name, expected", [
        ("a.mp4", "video"),
        ("a.MP4", "video"),
        ("a.mov", "video"),
        ("a.png", "image"),
        ("a.JPEG", "image"),
        ("a.txt", "other"),
        ("noext", "other"),
    ])
    def test_classifies_by_extension(self, name, expected):
        assert media.kind_of(name) == expected


class TestAvailabilityProbe:
    def test_reports_missing_binaries(self, monkeypatch):
        monkeypatch.setattr(media.shutil, "which", lambda _name: None)
        assert media.have_ffmpeg() is False
        assert media.have_ffprobe() is False

    def test_reports_present_binaries(self, monkeypatch):
        monkeypatch.setattr(media.shutil, "which", lambda name: f"/usr/bin/{name}")
        assert media.have_ffmpeg() is True
        assert media.have_ffprobe() is True


class TestProbe:
    def test_without_ffprobe_returns_empty_shape(self, tmp_path, no_ffmpeg):
        path = tmp_path / "a.mp4"
        path.write_bytes(b"\x00")
        assert media.probe(str(path)) == EMPTY_INFO

    def test_missing_file_returns_empty_shape(self, tmp_path):
        assert media.probe(str(tmp_path / "nope.mp4")) == EMPTY_INFO

    def test_broken_media_returns_empty_shape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media, "_run", lambda *_a, **_k: (1, "", "boom"))
        path = tmp_path / "a.mp4"
        path.write_bytes(b"\x00")
        assert media.probe(str(path)) == EMPTY_INFO

    def test_parses_ffprobe_json(self, tmp_path, monkeypatch):
        payload = (
            '{"streams": [{"codec_type": "video", "width": 1080, "height": 1920},'
            ' {"codec_type": "audio"}], "format": {"duration": "12.34"}}'
        )
        monkeypatch.setattr(media, "_run", lambda *_a, **_k: (0, payload, ""))
        path = tmp_path / "a.mp4"
        path.write_bytes(b"\x00")
        assert media.probe(str(path)) == {"duration_s": 12.34, "width": 1080,
                                          "height": 1920, "has_audio": True}

    def test_garbage_json_returns_empty_shape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media, "_run", lambda *_a, **_k: (0, "not json", ""))
        path = tmp_path / "a.mp4"
        path.write_bytes(b"\x00")
        assert media.probe(str(path)) == EMPTY_INFO


class TestExtractKeyframes:
    def test_without_ffmpeg_returns_empty(self, tmp_path, no_ffmpeg):
        path = tmp_path / "a.mp4"
        path.write_bytes(b"\x00")
        assert media.extract_keyframes(str(path), str(tmp_path / "kf")) == []

    def test_missing_file_returns_empty(self, tmp_path):
        assert media.extract_keyframes(str(tmp_path / "nope.mp4"), str(tmp_path / "kf")) == []


class TestRunHelper:
    def test_timeout_is_reported_not_raised(self, monkeypatch):
        def fake_run(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=1)

        monkeypatch.setattr(media.subprocess, "run", fake_run)
        code, out, err = media._run(["ffprobe"])
        assert code == -1
        assert out == ""
        assert "TimeoutExpired" in err

    def test_missing_binary_is_reported_not_raised(self, monkeypatch):
        def fake_run(*_args, **_kwargs):
            raise FileNotFoundError("no ffprobe")

        monkeypatch.setattr(media.subprocess, "run", fake_run)
        code, _out, err = media._run(["ffprobe"])
        assert code == -1
        assert "FileNotFoundError" in err

    def test_success_returns_decoded_streams(self, monkeypatch):
        class FakeProc:
            returncode = 0
            stdout = "输出".encode()
            stderr = b""

        monkeypatch.setattr(media.subprocess, "run", lambda *_a, **_k: FakeProc())
        assert media._run(["ffprobe"]) == (0, "输出", "")
