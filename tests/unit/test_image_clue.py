"""图片热点解析（S6.6）单测：magic bytes、体积上限、能力开关与上游失败分支。

真的多模态调用留给集成与真机冒烟；这里用 `httpx.MockTransport` 把请求形状与失败映射钉死。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from xhs_agent.api.deps import get_config
from xhs_agent.api.main import create_app
from xhs_agent.api.routers import analysis as analysis_router
from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.errors import DependencyUnavailableError, UpstreamError
from xhs_agent.services import image_clue as image_clue_service
from xhs_agent.services.image_clue import parse_image_clue
from xhs_agent.tools.vision import sniff_image_mime

# 三种受支持格式的最小字节头（内容本身不需要是真图片：校验只看 magic bytes）
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF" + (32).to_bytes(4, "little") + b"WEBP" + b"\x00" * 32

CLUE_PAYLOAD: dict[str, Any] = {
    "raw_text": "夜跑打卡被拍：路灯下的塑胶跑道，一个人背对镜头慢跑",
    "clue": {
        "why_it_works": ["自律夜跑与路边宵夜并置，形成反差"],
        "mechanisms": [{"name": "反差", "explain": "自律与放纵同框"}],
        "elements": [{"type": "scene", "value": "夜晚跑道", "weight": 0.9,
                      "confidence": 0.9, "evidence": "路灯下的塑胶跑道"}],
        "match_keywords": ["夜跑", "夜景"],
        "borrow_angles": ["用同款夜景跑道做热场"],
    },
}


def build_config(tmp_path, monkeypatch, *, enabled: bool = True,
                 key: str | None = "sk-vision", model: str = "qwen-vl-max") -> AppConfig:
    """临时配置：`[vision]` 按需打开，密钥走自定义变量名（不读真实 config/.env）。"""
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        f'[vision]\nenabled = {str(enabled).lower()}\napi_key_env = "MY_VISION_KEY"\n'
        f'model = "{model}"\n'
        "[frontend]\nserve = false\n",
        encoding="utf-8")
    if key is None:
        monkeypatch.delenv("MY_VISION_KEY", raising=False)
    else:
        monkeypatch.setenv("MY_VISION_KEY", key)
    return load_config(str(toml_path))


def _capture_request(request: httpx.Request, capture: dict[str, Any] | None) -> None:
    if capture is None:
        return
    capture["url"] = str(request.url)
    capture["payload"] = json.loads(request.content.decode("utf-8"))
    capture["authorization"] = request.headers.get("Authorization")


def model_transport(model_text: str,
                    capture: dict[str, Any] | None = None) -> httpx.MockTransport:
    """假多模态端点：把 `model_text` 包成 OpenAI 兼容的 choices[0].message.content。"""
    def handler(request: httpx.Request) -> httpx.Response:
        _capture_request(request, capture)
        return httpx.Response(200, json={"choices": [{"message": {"content": model_text}}]})
    return httpx.MockTransport(handler)


def error_transport(status: int, text: str = "") -> httpx.MockTransport:
    """假多模态端点：直接返回 HTTP 错误。"""
    def handler(request: httpx.Request) -> httpx.Response:
        _capture_request(request, None)
        return httpx.Response(status, text=text or "upstream boom")
    return httpx.MockTransport(handler)


class TestSniffImageMime:
    @pytest.mark.parametrize(("data", "mime"), [
        (JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp"),
    ])
    def test_recognizes_supported_formats(self, data, mime):
        assert sniff_image_mime(data) == mime

    @pytest.mark.parametrize("data", [b"", b"GIF89a....", b"<html></html>", b"RIFFxxxxNOPE"])
    def test_rejects_other_content(self, data):
        assert sniff_image_mime(data) == ""


class TestParseImageClue:
    @pytest.mark.asyncio
    async def test_posts_multimodal_payload_and_parses_clue(self, tmp_path, monkeypatch):
        cfg = build_config(tmp_path, monkeypatch)
        capture: dict[str, Any] = {}
        model_text = json.dumps(CLUE_PAYLOAD, ensure_ascii=False)
        async with httpx.AsyncClient(transport=model_transport(model_text, capture)) as client:
            parsed = await parse_image_clue(cfg, JPEG, "image/jpeg", http=client)

        assert parsed.raw_text == CLUE_PAYLOAD["raw_text"]
        assert parsed.clue.elements[0].value == "夜晚跑道"
        assert parsed.prompt_versions == {"image_hotspot_clue": 1}

        payload = capture["payload"]
        assert capture["url"].endswith("/chat/completions")
        assert payload["model"] == "qwen-vl-max"
        assert payload["response_format"] == {"type": "json_object"}
        assert "不猜身份" in payload["messages"][0]["content"]        # 01+03 进了 system
        image_part = payload["messages"][1]["content"][1]
        assert image_part["type"] == "image_url"
        assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert capture["authorization"] == "Bearer sk-vision"

    @pytest.mark.asyncio
    async def test_disabled_vision_is_dependency_unavailable(self, tmp_path, monkeypatch):
        cfg = build_config(tmp_path, monkeypatch, enabled=False)
        with pytest.raises(DependencyUnavailableError) as info:
            await parse_image_clue(cfg, JPEG, "image/jpeg")
        assert info.value.code == "dependency_unavailable"

    @pytest.mark.asyncio
    async def test_missing_key_is_dependency_unavailable(self, tmp_path, monkeypatch):
        cfg = build_config(tmp_path, monkeypatch, key=None)
        with pytest.raises(DependencyUnavailableError):
            await parse_image_clue(cfg, JPEG, "image/jpeg")

    @pytest.mark.asyncio
    async def test_http_error_is_upstream_error(self, tmp_path, monkeypatch):
        cfg = build_config(tmp_path, monkeypatch)
        async with httpx.AsyncClient(transport=error_transport(500, "模型炸了")) as client:
            with pytest.raises(UpstreamError) as info:
                await parse_image_clue(cfg, JPEG, "image/jpeg", http=client)
        assert "HTTP 500" in info.value.message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [
        None,                                                              # 非 JSON
        {"clue": CLUE_PAYLOAD["clue"]},                                    # 缺 raw_text
        {"raw_text": "只有描述"},                                           # 缺 clue
        {"raw_text": "x" * 501, "clue": CLUE_PAYLOAD["clue"]},              # 描述超长
        {"raw_text": "有描述", "clue": {**CLUE_PAYLOAD["clue"], "elements": []}},  # 没要素
        {"raw_text": "有描述", "clue": {"elements": [{"type": "topic"}]}},   # 要素缺 value
    ])
    async def test_bad_model_output_is_upstream_error(self, tmp_path, monkeypatch, payload):
        cfg = build_config(tmp_path, monkeypatch)
        body = "not json at all" if payload is None else json.dumps(payload, ensure_ascii=False)
        async with httpx.AsyncClient(transport=model_transport(body)) as client:
            with pytest.raises(UpstreamError):
                await parse_image_clue(cfg, JPEG, "image/jpeg", http=client)


class FakeVisionClient:
    """假的多模态客户端：直接回一段预置 JSON（不碰网络）。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def complete(self, rendered, data: bytes, mime: str) -> str:
        self.calls += 1
        return self.text


class TestImageClueEndpoint:
    @staticmethod
    def _client(cfg: AppConfig) -> TestClient:
        app = create_app(cfg, check_startup=False)
        app.dependency_overrides[get_config] = lambda: cfg
        return TestClient(app, raise_server_exceptions=False)

    def test_upload_returns_contract_shape(self, tmp_path, monkeypatch):
        cfg = build_config(tmp_path, monkeypatch)
        fake = FakeVisionClient(json.dumps(CLUE_PAYLOAD, ensure_ascii=False))
        monkeypatch.setattr(image_clue_service, "build_image_client",
                            lambda _cfg, *, client: fake)

        with self._client(cfg) as client:
            response = client.post("/api/hotspots/image-clue",
                                   files={"file": ("hot.jpg", JPEG, "image/jpeg")})

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"raw_text", "clue", "prompt_versions"}
        assert body["prompt_versions"] == {"image_hotspot_clue": 1}
        assert body["clue"]["elements"][0]["type"] == "scene"
        assert fake.calls == 1

    @pytest.mark.parametrize(("name", "data", "content_type"), [
        ("note.txt", b"hello world", "text/plain"),
        ("fake.png", b"GIF89a....", "image/png"),      # 声明的类型不可信
        ("empty.jpg", b"", "image/jpeg"),
    ])
    def test_rejects_non_image_uploads(self, tmp_path, monkeypatch, name, data, content_type):
        cfg = build_config(tmp_path, monkeypatch)
        with self._client(cfg) as client:
            response = client.post("/api/hotspots/image-clue",
                                   files={"file": (name, data, content_type)})
        assert response.status_code == 400
        assert response.json()["code"] == "bad_request"

    def test_rejects_oversize_upload(self, tmp_path, monkeypatch):
        cfg = build_config(tmp_path, monkeypatch)
        monkeypatch.setattr(analysis_router, "MAX_IMAGE_BYTES", 128)
        with self._client(cfg) as client:
            response = client.post("/api/hotspots/image-clue",
                                   files={"file": ("big.jpg", JPEG + b"\x00" * 256,
                                                   "image/jpeg")})
        assert response.status_code == 400
        assert "上限" in response.json()["message"]

    def test_disabled_vision_is_503(self, tmp_path, monkeypatch):
        cfg = build_config(tmp_path, monkeypatch, enabled=False)
        with self._client(cfg) as client:
            response = client.post("/api/hotspots/image-clue",
                                   files={"file": ("hot.jpg", JPEG, "image/jpeg")})
        assert response.status_code == 503
        assert response.json()["code"] == "dependency_unavailable"
