"""视觉打标单测：只覆盖「能力裁剪」的三种情形与 base64 编码，真调用留给集成测试。"""

from __future__ import annotations

import base64

import pytest

from xhs_agent.config import load_config
from xhs_agent.tools import vision


def build_config(tmp_path, monkeypatch, *, enabled: bool, key: str | None):
    """按开关与密钥两种情形构造配置，不读真实 config/.env。"""
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        f'[vision]\nenabled = {str(enabled).lower()}\napi_key_env = "MY_VISION_KEY"\n',
        encoding="utf-8")
    if key is not None:
        monkeypatch.setenv("MY_VISION_KEY", key)
    return load_config(str(toml_path))


class TestMakeDescriberGate:
    def test_disabled_returns_none(self, tmp_path, monkeypatch):
        config = build_config(tmp_path, monkeypatch, enabled=False, key="k")
        assert vision.make_describer(config) is None

    def test_enabled_without_key_returns_none(self, tmp_path, monkeypatch):
        config = build_config(tmp_path, monkeypatch, enabled=True, key=None)
        assert vision.make_describer(config) is None

    def test_enabled_with_key_but_no_ffmpeg_returns_none(self, tmp_path, monkeypatch):
        config = build_config(tmp_path, monkeypatch, enabled=True, key="k")
        monkeypatch.setattr("xhs_agent.config.shutil.which", lambda _name: None)
        assert vision.make_describer(config) is None

    def test_ready_config_returns_callable(self, tmp_path, monkeypatch):
        config = build_config(tmp_path, monkeypatch, enabled=True, key="k")
        monkeypatch.setattr("xhs_agent.config.shutil.which", lambda _name: "/usr/bin/ffmpeg")
        assert callable(vision.make_describer(config))


class TestDataUrl:
    def test_encodes_file_as_base64_data_url(self, tmp_path):
        path = tmp_path / "frame.jpg"
        payload = b"\xff\xd8\xff\xe0binary"
        path.write_bytes(payload)
        url = vision._data_url(str(path))
        prefix = "data:image/jpeg;base64,"
        assert url.startswith(prefix)
        assert base64.b64decode(url[len(prefix):]) == payload

    def test_missing_file_raises_oserror(self, tmp_path):
        with pytest.raises(OSError):
            vision._data_url(str(tmp_path / "nope.jpg"))
