"""图片热点解析（S6.6）单测：magic bytes、体积上限、能力开关与上游失败分支。

真的多模态调用留给集成与真机冒烟；这里用 `httpx.MockTransport` 把请求形状与失败映射钉死。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from xhs_agent.api.deps import get_config
from xhs_agent.api.main import create_app
from xhs_agent.api.routers import analysis as analysis_router
from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.errors import DependencyUnavailableError, UpstreamError
from xhs_agent.core.tracing import Generation, NullTracer
from xhs_agent.services import image_clue as image_clue_service
from xhs_agent.services.image_clue import parse_image_clue, parse_image_clue_multi
from xhs_agent.tools.vision import ImageCompletion, VisionError, sniff_image_mime

# 三种受支持格式的最小字节头（内容本身不需要是真图片：校验只看 magic bytes）
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF" + (32).to_bytes(4, "little") + b"WEBP" + b"\x00" * 32

CLUE_PAYLOAD: dict[str, Any] = {
    "raw_text": "夜跑打卡被拍：路灯下的塑胶跑道，一个人背对镜头慢跑",
    "clue": {
        "why_it_works": ["自律夜跑与路边宵夜并置，形成反差"],
        "mechanisms": [{"name": "反差", "explain": "自律与放纵同框"}],
        "elements": [
            {
                "type": "scene",
                "value": "夜晚跑道",
                "weight": 0.9,
                "confidence": 0.9,
                "evidence": "路灯下的塑胶跑道",
            }
        ],
        "match_keywords": ["夜跑", "夜景"],
        "borrow_angles": ["用同款夜景跑道做热场"],
    },
}


def build_config(
    tmp_path,
    monkeypatch,
    *,
    enabled: bool = True,
    key: str | None = "sk-vision",
    model: str = "qwen3-vl-plus",
) -> AppConfig:
    """临时配置：`[vision]` 按需打开，密钥走自定义变量名（不读真实 config/.env）。"""
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        f'[vision]\nenabled = {str(enabled).lower()}\napi_key_env = "MY_VISION_KEY"\n'
        f'model = "{model}"\n'
        "[frontend]\nserve = false\n",
        encoding="utf-8",
    )
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


def model_transport(
    model_text: str, capture: dict[str, Any] | None = None
) -> httpx.MockTransport:
    """假多模态端点：把 `model_text` 包成 OpenAI 兼容的 choices[0].message.content。"""

    def handler(request: httpx.Request) -> httpx.Response:
        _capture_request(request, capture)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": model_text}}]}
        )

    return httpx.MockTransport(handler)


def error_transport(status: int, text: str = "") -> httpx.MockTransport:
    """假多模态端点：直接返回 HTTP 错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        _capture_request(request, None)
        return httpx.Response(status, text=text or "upstream boom")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_invalid_vision_schema_keeps_actual_usage_on_failed_tool(tmp_path, monkeypatch):
    from xhs_agent.services import chat_tools
    from xhs_agent.services.image_clue import parse_image_clue

    cfg = build_config(tmp_path, monkeypatch)
    invalid = {"raw_text": "夜跑", "clue": {**CLUE_PAYLOAD["clue"],
               "elements": [{"type": "text", "value": "错误类型"}]}}

    def transport(_request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(invalid)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(UpstreamError) as info:
            await parse_image_clue(cfg, PNG, "image/png", http=client)
    usage = chat_tools._failed_image_usage(info.value)
    assert usage["usage_status"] == "actual"
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (100, 20)
    assert usage["cost_cny"] > 0
    assert chat_tools._failed_image_versions(info.value) == {"image_hotspot_clue": 2}


class TestSniffImageMime:
    @pytest.mark.parametrize(
        ("data", "mime"),
        [
            (JPEG, "image/jpeg"),
            (PNG, "image/png"),
            (WEBP, "image/webp"),
        ],
    )
    def test_recognizes_supported_formats(self, data, mime):
        assert sniff_image_mime(data) == mime

    @pytest.mark.parametrize(
        "data", [b"", b"GIF89a....", b"<html></html>", b"RIFFxxxxNOPE"]
    )
    def test_rejects_other_content(self, data):
        assert sniff_image_mime(data) == ""


class TestParseImageClue:
    @pytest.mark.asyncio
    async def test_posts_multimodal_payload_and_parses_clue(
        self, tmp_path, monkeypatch
    ):
        cfg = build_config(tmp_path, monkeypatch)
        capture: dict[str, Any] = {}
        model_text = json.dumps(CLUE_PAYLOAD, ensure_ascii=False)
        async with httpx.AsyncClient(
            transport=model_transport(model_text, capture)
        ) as client:
            parsed = await parse_image_clue(cfg, JPEG, "image/jpeg", http=client)

        assert parsed.raw_text == CLUE_PAYLOAD["raw_text"]
        assert parsed.clue.elements[0].value == "夜晚跑道"
        assert parsed.prompt_versions == {"image_hotspot_clue": 2}

        payload = capture["payload"]
        assert capture["url"].endswith("/chat/completions")
        assert payload["model"] == "qwen3-vl-plus"
        assert payload["response_format"] == {"type": "json_object"}
        assert "不猜身份" in payload["messages"][0]["content"]  # 01+03 进了 system
        image_part = payload["messages"][1]["content"][1]
        assert image_part["type"] == "image_url"
        assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert capture["authorization"] == "Bearer sk-vision"

    @pytest.mark.asyncio
    async def test_disabled_vision_is_dependency_unavailable(
        self, tmp_path, monkeypatch
    ):
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
        async with httpx.AsyncClient(
            transport=error_transport(500, "模型炸了")
        ) as client:
            with pytest.raises(UpstreamError) as info:
                await parse_image_clue(cfg, JPEG, "image/jpeg", http=client)
        assert "HTTP 500" in info.value.message

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            None,  # 非 JSON
            {"clue": CLUE_PAYLOAD["clue"]},  # 缺 raw_text
            {"raw_text": "只有描述"},  # 缺 clue
            {"raw_text": "x" * 501, "clue": CLUE_PAYLOAD["clue"]},  # 描述超长
            {
                "raw_text": "有描述",
                "clue": {**CLUE_PAYLOAD["clue"], "elements": []},
            },  # 没要素
            {
                "raw_text": "有描述",
                "clue": {"elements": [{"type": "topic"}]},
            },  # 要素缺 value
        ],
    )
    async def test_bad_model_output_is_upstream_error(
        self, tmp_path, monkeypatch, payload
    ):
        cfg = build_config(tmp_path, monkeypatch)
        body = (
            "not json at all"
            if payload is None
            else json.dumps(payload, ensure_ascii=False)
        )
        async with httpx.AsyncClient(transport=model_transport(body)) as client:
            with pytest.raises(UpstreamError):
                await parse_image_clue(cfg, JPEG, "image/jpeg", http=client)


MULTI_CLUE_PAYLOAD: dict[str, Any] = {
    "raw_text": "球拍、球场与球鞋三张近景拼出一场社区羽毛球",
    "clue": {
        "why_it_works": ["器材特写与球场镜头并置，专业感与日常感形成反差"],
        "mechanisms": [{"name": "反差", "explain": "器材特写与普通球场并存"}],
        "elements": [
            {
                "type": "visual",
                "value": "球拍特写",
                "weight": 0.9,
                "confidence": 0.9,
                "evidence": "图1：球拍与手背的特写",
            },
            {
                "type": "scene",
                "value": "社区球场",
                "weight": 0.8,
                "confidence": 0.85,
                "evidence": "图2：场地全景，有人在打球",
            },
            {
                "type": "visual",
                "value": "地面线条",
                "weight": 0.35,
                "confidence": 0.6,
                "evidence": "图3：球鞋踩在场线边",
            },
        ],
        "match_keywords": ["羽毛球", "球拍", "球场"],
        "borrow_angles": ["用器材特写开场，再切场上镜头"],
    },
}


def _multi_text(elements: list[dict[str, Any]]) -> str:
    """按给定要素拼一份多图解析的模型输出（用例里只改要素部分）。"""
    payload = json.loads(json.dumps(MULTI_CLUE_PAYLOAD))
    payload["clue"]["elements"] = elements
    return json.dumps(payload, ensure_ascii=False)


class TestParseImageClueMulti:
    """多图合并解析（S7.15）：一次调用出一份线索 + 多图专属校验。"""

    @pytest.mark.asyncio
    async def test_posts_every_image_in_order_and_returns_one_clue(
        self, tmp_path, monkeypatch
    ) -> None:
        cfg = build_config(tmp_path, monkeypatch)
        capture: dict[str, Any] = {}
        images = [(JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp")]

        async with httpx.AsyncClient(
            transport=model_transport(
                _multi_text(MULTI_CLUE_PAYLOAD["clue"]["elements"]), capture
            )
        ) as client:
            parsed = await parse_image_clue_multi(cfg, images, http=client)

        assert parsed.raw_text == MULTI_CLUE_PAYLOAD["raw_text"]
        assert parsed.clue.elements[0].value == "球拍特写"
        assert parsed.prompt_versions == {"image_hotspot_clue_multi": 2}
        assert parsed.prompt_tokens > 0 and parsed.cost_cny >= 0

        payload = capture["payload"]
        # 用的是多图那份 prompt（合并口径在 03 Constraints 里）
        assert "共同优先" in payload["messages"][0]["content"]
        content = payload["messages"][1]["content"]
        assert content[0]["type"] == "text"
        order = [item["image_url"]["url"].split(";")[0] for item in content[1:]]
        assert order == ["data:image/jpeg", "data:image/png", "data:image/webp"]

    @pytest.mark.asyncio
    async def test_disabled_vision_is_dependency_unavailable(
        self, tmp_path, monkeypatch
    ) -> None:
        cfg = build_config(tmp_path, monkeypatch, enabled=False)
        with pytest.raises(DependencyUnavailableError):
            await parse_image_clue_multi(cfg, [(JPEG, "image/jpeg")])

    @pytest.mark.asyncio
    async def test_empty_image_list_is_rejected(self, tmp_path, monkeypatch) -> None:
        cfg = build_config(tmp_path, monkeypatch)
        with pytest.raises(ValueError):
            await parse_image_clue_multi(cfg, [])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [2, 9])
    async def test_element_count_out_of_range_is_upstream_error(
        self, tmp_path, monkeypatch, count: int
    ) -> None:
        cfg = build_config(tmp_path, monkeypatch)
        element = MULTI_CLUE_PAYLOAD["clue"]["elements"][0]
        text = _multi_text([dict(element) for _ in range(count)])
        async with httpx.AsyncClient(transport=model_transport(text)) as client:
            with pytest.raises(UpstreamError) as info:
                await parse_image_clue_multi(cfg, [(JPEG, "image/jpeg")], http=client)
        assert "要素数量" in info.value.message

    @pytest.mark.asyncio
    async def test_missing_evidence_is_upstream_error(
        self, tmp_path, monkeypatch
    ) -> None:
        cfg = build_config(tmp_path, monkeypatch)
        elements = [dict(item) for item in MULTI_CLUE_PAYLOAD["clue"]["elements"]]
        elements[1]["evidence"] = ""
        async with httpx.AsyncClient(
            transport=model_transport(_multi_text(elements))
        ) as client:
            with pytest.raises(UpstreamError) as info:
                await parse_image_clue_multi(cfg, [(JPEG, "image/jpeg")], http=client)
        assert "缺少 evidence" in info.value.message

    @pytest.mark.asyncio
    async def test_upstream_http_error_is_reported(self, tmp_path, monkeypatch) -> None:
        cfg = build_config(tmp_path, monkeypatch)
        async with httpx.AsyncClient(
            transport=error_transport(500, "模型炸了")
        ) as client:
            with pytest.raises(UpstreamError) as info:
                await parse_image_clue_multi(cfg, [(JPEG, "image/jpeg")], http=client)
        assert "HTTP 500" in info.value.message


class FakeVisionClient:
    """假的多模态客户端：直接回一段预置 JSON（不碰网络）。"""

    def __init__(
        self,
        text: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        usage_status: str = "estimated",
    ) -> None:
        self.text = text
        self.calls = 0
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.usage_status = usage_status  # R5（E7OBS2-04）：用量来源口径随结果上卷

    async def complete(self, rendered, data: bytes, mime: str) -> ImageCompletion:
        self.calls += 1
        return ImageCompletion(
            text=self.text,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            usage_status=self.usage_status,
        )

    async def complete_many(self, rendered, images) -> ImageCompletion:
        self.calls += 1
        return ImageCompletion(
            text=self.text,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            usage_status=self.usage_status,
        )


class TestImageClueEndpoint:
    @staticmethod
    def _client(cfg: AppConfig) -> TestClient:
        app = create_app(cfg, check_startup=False)
        app.dependency_overrides[get_config] = lambda: cfg
        return TestClient(app, raise_server_exceptions=False)

    def test_upload_returns_contract_shape(self, tmp_path, monkeypatch):
        cfg = build_config(tmp_path, monkeypatch)
        fake = FakeVisionClient(json.dumps(CLUE_PAYLOAD, ensure_ascii=False))
        monkeypatch.setattr(
            image_clue_service, "build_image_client", lambda _cfg, *, client: fake
        )

        with self._client(cfg) as client:
            response = client.post(
                "/api/hotspots/image-clue",
                files={"file": ("hot.jpg", JPEG, "image/jpeg")},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"raw_text", "clue", "prompt_versions"}
        assert body["prompt_versions"] == {"image_hotspot_clue": 2}
        assert body["clue"]["elements"][0]["type"] == "scene"
        assert fake.calls == 1

    @pytest.mark.asyncio
    async def test_image_parse_cost_is_charged_with_vision_prices(
        self, tmp_path, monkeypatch
    ):
        """S7.10：图片解析不再"不计成本"——用量按 `[vision].price_*` 折成人民币元。"""
        cfg = build_config(tmp_path, monkeypatch)
        fake = FakeVisionClient(
            json.dumps(CLUE_PAYLOAD, ensure_ascii=False),
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
        )
        monkeypatch.setattr(
            image_clue_service, "build_image_client", lambda _cfg, *, client: fake
        )
        parsed = await parse_image_clue(cfg, JPEG, "image/jpeg")
        assert parsed.prompt_tokens == 1_000_000
        assert parsed.completion_tokens == 1_000_000
        # 3.0 + 9.0 元/百万 token（[vision] 默认单价）
        assert parsed.cost_cny == pytest.approx(12.0)

    @pytest.mark.parametrize(
        ("name", "data", "content_type"),
        [
            ("note.txt", b"hello world", "text/plain"),
            ("fake.png", b"GIF89a....", "image/png"),  # 声明的类型不可信
            ("empty.jpg", b"", "image/jpeg"),
        ],
    )
    def test_rejects_non_image_uploads(
        self, tmp_path, monkeypatch, name, data, content_type
    ):
        cfg = build_config(tmp_path, monkeypatch)
        with self._client(cfg) as client:
            response = client.post(
                "/api/hotspots/image-clue", files={"file": (name, data, content_type)}
            )
        assert response.status_code == 400
        assert response.json()["code"] == "bad_request"

    def test_rejects_oversize_upload(self, tmp_path, monkeypatch):
        cfg = build_config(tmp_path, monkeypatch)
        monkeypatch.setattr(analysis_router, "MAX_IMAGE_BYTES", 128)
        with self._client(cfg) as client:
            response = client.post(
                "/api/hotspots/image-clue",
                files={"file": ("big.jpg", JPEG + b"\x00" * 256, "image/jpeg")},
            )
        assert response.status_code == 400
        assert "上限" in response.json()["message"]

    def test_disabled_vision_is_503(self, tmp_path, monkeypatch):
        cfg = build_config(tmp_path, monkeypatch, enabled=False)
        with self._client(cfg) as client:
            response = client.post(
                "/api/hotspots/image-clue",
                files={"file": ("hot.jpg", JPEG, "image/jpeg")},
            )
        assert response.status_code == 503
        assert response.json()["code"] == "dependency_unavailable"


class _RecordingTracer(NullTracer):
    """捕获 generation 字段的假 tracer（R3 / OBS-06 验收用）。"""

    def __init__(self) -> None:
        self.generations: list[dict[str, Any]] = []

    def generation(self, *, name: str, model: str, metadata: dict[str, Any]):
        fields = Generation(metadata=dict(metadata))
        holder: dict[str, Any] = {"name": name, "model": model, "fields": fields}

        @contextmanager
        def _cm():
            try:
                yield fields
            finally:
                self.generations.append(holder)  # 异常路径也要记录（finally）

        return _cm()


class TestVisionGeneration:
    """R3（OBS-06）：真实视觉调用有等价 generation；一次多图调用只记一个；失败收尾安全。"""

    @pytest.mark.asyncio
    async def test_single_parse_emits_generation_with_usage_and_cost(
        self, tmp_path, monkeypatch
    ):
        cfg = build_config(tmp_path, monkeypatch)
        tracer = _RecordingTracer()
        monkeypatch.setattr(image_clue_service, "get_tracer", lambda _cfg: tracer)
        fake = FakeVisionClient(
            json.dumps(CLUE_PAYLOAD, ensure_ascii=False),
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
        )
        monkeypatch.setattr(
            image_clue_service, "build_image_client", lambda _cfg, *, client: fake
        )

        parsed = await parse_image_clue(cfg, JPEG, "image/jpeg")

        assert parsed.usage_status == "estimated"  # R5：来源口径随结果上卷
        assert len(tracer.generations) == 1
        gen = tracer.generations[0]
        assert gen["name"] == "image_hotspot_clue" and gen["model"] == cfg.vision.model
        assert gen["fields"].metadata["images"] == 1
        assert gen["fields"].usage_details == {
            "input": 1_000_000,
            "output": 1_000_000,
            "total": 2_000_000,
        }
        assert gen["fields"].cost_details["total"] == pytest.approx(12.0)
        assert (
            gen["fields"].metadata["usage_status"] == "estimated"
        )  # 假客户端没给 usage

    @pytest.mark.asyncio
    async def test_vendor_usage_marks_the_result_actual(self, tmp_path, monkeypatch):
        """R5（E7OBS2-04）：供应商给了 usage → 结果口径 actual，不冒充估算。"""
        cfg = build_config(tmp_path, monkeypatch)
        tracer = _RecordingTracer()
        monkeypatch.setattr(image_clue_service, "get_tracer", lambda _cfg: tracer)
        fake = FakeVisionClient(
            json.dumps(CLUE_PAYLOAD, ensure_ascii=False),
            prompt_tokens=300,
            completion_tokens=80,
            usage_status="actual",
        )
        monkeypatch.setattr(
            image_clue_service, "build_image_client", lambda _cfg, *, client: fake
        )

        parsed = await parse_image_clue(cfg, JPEG, "image/jpeg")

        assert parsed.usage_status == "actual"
        assert parsed.prompt_tokens == 300 and parsed.completion_tokens == 80

    @pytest.mark.asyncio
    async def test_multi_parse_counts_one_generation_for_the_group(
        self, tmp_path, monkeypatch
    ):
        """一组 3 张图 = **一个**逻辑 generation（一次多图调用一份用量，S7.15 口径）。"""
        cfg = build_config(tmp_path, monkeypatch)
        tracer = _RecordingTracer()
        monkeypatch.setattr(image_clue_service, "get_tracer", lambda _cfg: tracer)
        fake = FakeVisionClient(json.dumps(CLUE_PAYLOAD, ensure_ascii=False))
        monkeypatch.setattr(
            image_clue_service, "build_image_client", lambda _cfg, *, client: fake
        )

        # 多图专属校验：要素 3–8 个且都带 evidence —— 构造一份合规载荷
        multi_payload = dict(CLUE_PAYLOAD)
        clue = dict(CLUE_PAYLOAD["clue"])
        element = dict(clue["elements"][0])
        clue["elements"] = [
            element,
            dict(element, type="visual", value="夜跑的人"),
            dict(element, type="emotion", value="自律感"),
        ]
        multi_payload["clue"] = clue
        fake.text = json.dumps(multi_payload, ensure_ascii=False)
        parsed = await parse_image_clue_multi(
            cfg, [(JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp")]
        )

        assert parsed.usage_status == "estimated"
        assert len(tracer.generations) == 1
        gen = tracer.generations[0]
        assert gen["name"] == "image_hotspot_clue_multi"
        assert gen["fields"].metadata["images"] == 3

    @pytest.mark.asyncio
    async def test_vision_failure_marks_error_with_unknown_usage(
        self, tmp_path, monkeypatch
    ):
        """上游失败：generation 标 ERROR + 安全类别；用量如实 unknown，不填零。"""
        cfg = build_config(tmp_path, monkeypatch)
        tracer = _RecordingTracer()
        monkeypatch.setattr(image_clue_service, "get_tracer", lambda _cfg: tracer)

        class BrokenClient:
            async def complete(
                self, rendered, data: bytes, mime: str
            ) -> ImageCompletion:
                raise VisionError("上游 502")

        monkeypatch.setattr(
            image_clue_service,
            "build_image_client",
            lambda _cfg, *, client: BrokenClient(),
        )

        with pytest.raises(UpstreamError):
            await parse_image_clue(cfg, JPEG, "image/jpeg")

        gen = tracer.generations[0]
        assert gen["fields"].level == "ERROR"
        assert gen["fields"].status_message == "llm_error"  # 不含上游正文（OBS-02）
        assert gen["fields"].metadata["usage_status"] == "unknown"
        assert gen["fields"].usage_details == {}  # 没有用量就不写
