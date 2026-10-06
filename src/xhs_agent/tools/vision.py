"""多模态模型的两条路径：素材关键帧打标（同步）与图片热点解析（异步）。

**同步路径**（素材打标，S2.5 起）：`make_describer(cfg)` → 给 `services/materials.sync_materials` 用，
走标准库 `urllib`；S3.5 显式保留它（建索引路径、异步化收益低），本模块不改这条路径的行为。

**异步路径**（图片热点解析，S6.6）：`build_image_client(cfg)` → 给 `services/image_clue` 用，
走调用方注入的 httpx 连接池，在 API 请求里不阻塞事件循环。

两条路径共用同一份 prompt 渲染与「data URL + content 数组」的消息形状，但**能力判定的口径不同**：
同步路径缺能力返回 `None`（能力裁剪，索引退回文件名标签）；异步路径缺能力抛错（请求路径不做降级）。
另外异步路径**不要求 ffmpeg**——图片不抽帧，直接进模型。

输入：`AppConfig`（读 `[vision]` 段与密钥）。
输出：同步 `describe(frames, hint)` → `{"title", "tags", "description"}` 或 None；
      异步 `AsyncImageClient.complete(rendered, data, mime)` → 模型返回的文本（失败抛 `VisionError`）。
prompt 来源：`src/xhs_agent/prompts/material_tagging.md`（五段结构，见
`docs/contracts/prompt契约.md`）——本模块**不再内联 prompt 文本**。
能力裁剪：开关关闭、没有密钥、机器没有 ffmpeg 时 `make_describer` 直接返回 None，
索引会退回「文件名 + 人工说明」的标签体系——这是能力裁剪，不是运行时降级（AGENTS.md 第 4 节）。
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import httpx

from ..config import AppConfig
from ..util import extract_json
from . import prompt as prompt_tools

TASK_ID = "material_tagging"
IMAGE_TASK_ID = "image_hotspot_clue"

# 图片热点解析允许的图片类型：按 magic bytes 判断，不信声明的 content-type
_MAGIC_JPEG = b"\xff\xd8\xff"
_MAGIC_PNG = b"\x89PNG\r\n\x1a\n"


class VisionError(RuntimeError):
    """多模态调用失败：上游 HTTP 错误、超时、返回结构异常。"""


def sniff_image_mime(data: bytes) -> str:
    """按 magic bytes 认出 jpg / png / webp；认不出返回空串（调用方按 400 处理）。"""
    if data.startswith(_MAGIC_JPEG):
        return "image/jpeg"
    if data.startswith(_MAGIC_PNG):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def images_ready(cfg: AppConfig) -> bool:
    """图片热点解析的前置条件：`[vision].enabled` 打开且有密钥。

    **不要求 ffmpeg**：图片不抽帧，直接进模型（与素材打标的能力判定不同，
    后者用 `cfg.vision.available()`，含 ffmpeg）。
    """
    return bool(cfg.vision.enabled and cfg.vision.resolved_key())


def make_describer(cfg: AppConfig) -> Callable[[list[str], str], dict[str, Any] | None] | None:
    """返回一个 (frames, hint) -> dict|None 的函数；不可用时返回 None。"""
    if not cfg.vision.available():
        return None

    base_url = (cfg.vision.base_url or "").rstrip("/")
    api_key = cfg.vision.resolved_key()
    model = cfg.vision.model
    max_frames = max(1, cfg.vision.max_frames)

    def describe(frames: list[str], hint: str = "") -> dict[str, Any] | None:
        images = [f for f in frames[:max_frames] if os.path.exists(f)]
        if not images:
            return None
        # prompt 资产缺失或变量不全时直接抛 PromptError，不静默降级（契约 §四）
        rendered = prompt_tools.render(TASK_ID, file_name_hint=hint or "（未提供）")
        content: list[dict[str, Any]] = [{"type": "text", "text": rendered.user}]
        for path in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": _data_url(path)},
            })
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": rendered.system},
                {"role": "user", "content": content},
            ],
            "temperature": 0.2,
            "max_tokens": 600,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=cfg.vision.timeout_s) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
            text = data["choices"][0]["message"]["content"]
            parsed = extract_json(text)
        except (urllib.error.URLError, KeyError, IndexError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        tags = parsed.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.replace("，", ",").split(",")]
        return {
            "title": str(parsed.get("title") or "").strip(),
            "tags": [str(t).strip() for t in tags if str(t).strip()],
            "description": str(parsed.get("description") or "").strip(),
        }

    return describe


def _data_url(path: str) -> str:
    with open(path, "rb") as fh:
        return _data_url_from_bytes(fh.read(), "image/jpeg")


def _data_url_from_bytes(data: bytes, mime: str) -> str:
    payload = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{payload}"


class AsyncImageClient:
    """一张图片 → 多模态模型（OpenAI 兼容 `/chat/completions`，异步 httpx）。

    与素材打标的同步实现保持同样的消息形状（system = 01+03，user = 02+04+05 + 图片 content 元素），
    但**不重试**：`[vision]` 段没有重试预算，失败即抛 `VisionError` 由调用方折成 502——
    用户点「重解析」就是显式重试，成本也可见。
    """

    def __init__(self, cfg: Any, *, client: httpx.AsyncClient) -> None:
        self.cfg = cfg
        self.client = client
        self.base_url = (cfg.base_url or "").rstrip("/")
        self.api_key = cfg.resolved_key()
        if not self.base_url:
            raise VisionError("缺少 [vision].base_url，无法调用多模态模型")

    async def complete(self, rendered: prompt_tools.RenderedPrompt, data: bytes,
                       mime: str) -> str:
        """发一次多模态调用，返回模型文本；任何失败都抛 `VisionError`。"""
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": rendered.system},
                {"role": "user", "content": [
                    {"type": "text", "text": rendered.user},
                    {"type": "image_url", "image_url": {"url": _data_url_from_bytes(data, mime)}},
                ]},
            ],
            "temperature": 0.2,
            "max_tokens": 1200,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        try:
            response = await self.client.post(f"{self.base_url}/chat/completions", json=payload,
                                              headers=headers, timeout=self.cfg.timeout_s)
        except httpx.HTTPError as exc:
            raise VisionError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:300]
            raise VisionError(f"HTTP {response.status_code}: {detail or response.reason_phrase}")
        try:
            body = response.json()
        except ValueError as exc:
            raise VisionError(f"返回不是 JSON：{response.text[:200]}") from exc
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionError(f"返回结构异常：{str(body)[:200]}") from exc
        if not text:
            raise VisionError("返回内容为空")
        return str(text)


def build_image_client(cfg: AppConfig, *,
                       client: httpx.AsyncClient) -> AsyncImageClient | None:
    """按配置造异步图片客户端；`[vision]` 未启用或缺密钥时返回 None（调用方折成 503）。"""
    if not images_ready(cfg):
        return None
    return AsyncImageClient(cfg.vision, client=client)
