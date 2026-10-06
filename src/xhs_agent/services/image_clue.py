"""图片热点解析（S6.6）：一张图 → 热点描述 + 爆点线索。

用途：给 `POST /api/hotspots/image-clue` 用。返回的线索**不落库**——用户确认（可编辑）后
      随 `POST /api/analyze` 的 `clues` 字段提交（S6.7），那时才写进 `hotspots.clue`。
输入：`AppConfig`、图片字节与按 magic bytes 认出的 MIME；`http` 可注入（测试复用一个连接池）。
输出：`ImageClueParse`（`raw_text` / `clue` / `prompt_versions`）。

口径：

- `[vision].enabled = false` 或缺密钥 → `DependencyUnavailableError`（503，明确文案）；
  **不要求 ffmpeg**——图片不抽帧，直接进模型。
- 上游 HTTP 失败 / 超时 / 返回不是 JSON / 缺少 `clue` / 线索结构不合 `HotspotClue` 契约 / 要素为空
  → `UpstreamError`（502）。**不重试**：`[vision]` 段没有重试预算，用户点「重解析」就是显式重试。
- 图片本身只在这一个函数里变成模型输入，**不落盘、不入库**。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import AppConfig
from ..core.errors import DependencyUnavailableError, UpstreamError
from ..schemas import HotspotClue, SchemaError
from ..tools import prompt as prompt_tool
from ..tools.http import build_http_client
from ..tools.vision import VisionError, build_image_client, images_ready
from ..util import extract_json

TASK_ID = "image_hotspot_clue"
MAX_RAW_TEXT_CHARS = 500


@dataclass(frozen=True)
class ImageClueParse:
    """一次图片解析的结果（领域对象；接口层再映射成契约的 `ImageClueResult`）。"""

    raw_text: str
    clue: HotspotClue
    prompt_versions: dict[str, int] = field(default_factory=dict)


@asynccontextmanager
async def _client_or_stack(cfg: AppConfig,
                           http: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    """有注入的客户端就借用；没有就起一个短生命周期的（独立调用路径）。"""
    if http is not None:
        yield http
        return
    async with build_http_client(cfg.vision.timeout_s) as client:
        yield client


async def parse_image_clue(cfg: AppConfig, data: bytes, mime: str, *,
                           http: httpx.AsyncClient | None = None) -> ImageClueParse:
    """把一张图片拆成热点描述与线索；失败按 503 / 502 抛出，不降级。"""
    if not images_ready(cfg):
        raise DependencyUnavailableError(
            "图片热点解析未启用：需要 [vision].enabled = true 与密钥",
            {"vision_enabled": cfg.vision.enabled, "api_key_env": cfg.vision.api_key_env})

    # prompt 资产缺失或格式不合契约时直接抛 PromptError（500），不静默降级
    rendered = prompt_tool.render(TASK_ID)
    async with _client_or_stack(cfg, http) as client:
        api = build_image_client(cfg, client=client)
        if api is None:   # pragma: no cover - images_ready 已经拦过一遍，这里只是兜底
            raise DependencyUnavailableError("图片热点解析未启用：缺少可用的多模态客户端")
        try:
            text = await api.complete(rendered, data, mime)
        except VisionError as exc:
            raise UpstreamError(f"图片解析失败：{exc}",
                                {"detail": str(exc)[:200]}) from exc

    payload = _parse_payload(text)
    raw_text = str(payload.get("raw_text") or "").strip()
    if not raw_text:
        raise UpstreamError("图片解析失败：模型没有给出热点描述（raw_text 为空）")
    if len(raw_text) > MAX_RAW_TEXT_CHARS:
        raise UpstreamError(
            f"图片解析失败：热点描述超过 {MAX_RAW_TEXT_CHARS} 字上限（{len(raw_text)} 字）")
    clue_payload = payload.get("clue")
    if not isinstance(clue_payload, dict):
        raise UpstreamError("图片解析失败：返回里缺少 clue 对象")
    try:
        clue = HotspotClue.from_dict(clue_payload, hotspot_raw=raw_text)
    except SchemaError as exc:
        raise UpstreamError(f"图片解析失败：线索结构不合契约（{exc}）") from exc
    if not clue.elements:
        # 没有要素的"热点"检索不出任何素材，按上游失败处理（不返回半成品）
        raise UpstreamError("图片解析失败：模型没有给出任何爆点要素")
    return ImageClueParse(raw_text=raw_text, clue=clue,
                          prompt_versions={rendered.task_id: rendered.version})


def _parse_payload(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON 对象；不是对象就按上游失败处理。"""
    try:
        parsed = extract_json(text)
    except ValueError as exc:
        raise UpstreamError(f"图片解析失败：返回不是 JSON（{exc}）") from exc
    if not isinstance(parsed, dict):
        raise UpstreamError("图片解析失败：返回的不是 JSON 对象")
    return parsed
