"""图片热点解析（S6.6 单图 / S7.15 多图）：图 → 热点描述 + 爆点线索。

用途：给 `POST /api/hotspots/image-clue` 用。返回的线索**不落库**——用户确认（可编辑）后
      随 `POST /api/analyze` 的 `clues` 字段提交（S6.7），那时才写进 `hotspots.clue`。
      对话层另有两条：`parse_image_clue`（单图，工具 `parse_image_clue`）与
      `parse_image_clue_multi`（**一组 1–9 张**，一次调用合成一份线索，S7.15）。
输入：`AppConfig`、图片字节与按 magic bytes 认出的 MIME（多图是 `(bytes, mime)` 序列，按顺序）；
      `http` 可注入（测试复用一个连接池）。
输出：`ImageClueParse`（`raw_text` / `clue` / `prompt_versions` / 用量与成本）。

口径：

- `[vision].enabled = false` 或缺密钥 → `DependencyUnavailableError`（503，明确文案）；
  **不要求 ffmpeg**——图片不抽帧，直接进模型。
- **成本按 `[vision].price_in_per_m` / `price_out_per_m` 折算**（S7.10 起不再"不计成本"）：
  用量优先取上游 `usage`，缺失时按字符估算（下界）；结果里带回，由调用方算进对话轮级成本。
- 上游 HTTP 失败 / 超时 / 返回不是 JSON / 缺少 `clue` / 线索结构不合 `HotspotClue` 契约 / 要素为空
  → `UpstreamError`（502）。**不重试**：`[vision]` 段没有重试预算，用户点「重解析」就是显式重试。
- 图片本身只在这一个函数里变成模型输入，**不落盘、不入库**。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import AppConfig
from ..core.errors import DependencyUnavailableError, UpstreamError
from ..core.tracing import LEVEL_ERROR, get_tracer
from ..schemas import HotspotClue, SchemaError
from ..tools import prompt as prompt_tool
from ..tools.http import build_http_client
from ..tools.vision import (
    ImageCompletion,
    VisionError,
    build_image_client,
    images_ready,
)
from ..util import extract_json

TASK_ID = "image_hotspot_clue"
# 多图热点（S7.15）：一次调用合成一份线索，自己的 prompt 资产与校验口径
MULTI_TASK_ID = "image_hotspot_clue_multi"
MULTI_MIN_ELEMENTS = 3
MULTI_MAX_ELEMENTS = 8
MAX_RAW_TEXT_CHARS = 500


@dataclass(frozen=True)
class ImageClueParse:
    """一次图片解析的结果（领域对象；接口层再映射成契约的 `ImageClueResult`）。"""

    raw_text: str
    clue: HotspotClue
    prompt_versions: dict[str, int] = field(default_factory=dict)
    # S7.10：图片解析也算轮级成本——用量优先取上游 usage，缺失时按字符估算（下界）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    # R5（E7OBS2-04）：用量来源口径——actual（供应商给了 usage）/ estimated（按字符估算）/
    # cached（本轮缓存命中，无新增消费）。随结果上卷到工具 payload 与轮级汇总。
    usage_status: str = ""


def _cost_cny(cfg: AppConfig, completion: ImageCompletion) -> float:
    """按 `[vision]` 的单价把用量折成人民币元（与 `tools/llm.py` 同一记账口径）。"""
    return round(
        completion.prompt_tokens / 1_000_000 * cfg.vision.price_in_per_m
        + completion.completion_tokens / 1_000_000 * cfg.vision.price_out_per_m,
        4,
    )


def _vision_generation(
    cfg: AppConfig, *, task_id: str, version: int, image_count: int
) -> Any:
    """视觉调用的**等价 generation**（R3 / OBS-06）。

    真实视觉模型 + `[vision]` 单价；**一次多图调用只记一个逻辑 generation**；缓存命中不进
    这里（不虚构模型调用、不新增费用）。隔离与载荷规则复用 R1（OBS-01 / 02，门面内部已兜底）。
    """
    return get_tracer(cfg).generation(
        name=task_id,
        model=cfg.vision.model,
        metadata={
            "provider": "vision",
            "images": max(1, int(image_count)),
            "prompt_version": int(version),
        },
    )


def _fill_vision_usage(
    fields: Any, cfg: AppConfig, completion: ImageCompletion
) -> None:
    """把视觉调用的用量 / 成本写进 generation；用量口径随结果上卷（actual / estimated）。"""
    in_tokens = int(completion.prompt_tokens or 0)
    out_tokens = int(completion.completion_tokens or 0)
    fields.usage_details = {
        "input": in_tokens,
        "output": out_tokens,
        "total": in_tokens + out_tokens,
    }
    fields.cost_details = {
        "input": round(in_tokens / 1_000_000 * cfg.vision.price_in_per_m, 6),
        "output": round(out_tokens / 1_000_000 * cfg.vision.price_out_per_m, 6),
        "total": _cost_cny(cfg, completion),
    }
    fields.metadata["attempts"] = 1
    fields.metadata["usage_status"] = str(completion.usage_status or "estimated")


@asynccontextmanager
async def _client_or_stack(
    cfg: AppConfig, http: httpx.AsyncClient | None
) -> AsyncIterator[httpx.AsyncClient]:
    """有注入的客户端就借用；没有就起一个短生命周期的（独立调用路径）。"""
    if http is not None:
        yield http
        return
    async with build_http_client(cfg.vision.timeout_s) as client:
        yield client


async def parse_image_clue(
    cfg: AppConfig, data: bytes, mime: str, *, http: httpx.AsyncClient | None = None
) -> ImageClueParse:
    """把一张图片拆成热点描述与线索；失败按 503 / 502 抛出，不降级。"""
    if not images_ready(cfg):
        raise DependencyUnavailableError(
            "图片热点解析未启用：需要 [vision].enabled = true 与密钥",
            {
                "vision_enabled": cfg.vision.enabled,
                "api_key_env": cfg.vision.api_key_env,
            },
        )

    # prompt 资产缺失或格式不合契约时直接抛 PromptError（500），不静默降级
    rendered = prompt_tool.render(TASK_ID)
    async with _client_or_stack(cfg, http) as client:
        api = build_image_client(cfg, client=client)
        if api is None:  # pragma: no cover - images_ready 已经拦过一遍，这里只是兜底
            raise DependencyUnavailableError(
                "图片热点解析未启用：缺少可用的多模态客户端"
            )
        with _vision_generation(
            cfg, task_id=rendered.task_id, version=rendered.version, image_count=1
        ) as fields:
            try:
                completion = await api.complete(rendered, data, mime)
            except VisionError as exc:
                # R3（OBS-06 / OBS-02）：调用已发出、用量未知；错误只发安全类别
                fields.level = LEVEL_ERROR
                fields.status_message = "llm_error"
                fields.metadata["usage_status"] = "unknown"
                raise UpstreamError(
                    f"图片解析失败：{exc}", {"detail": str(exc)[:200]}
                ) from exc
            _fill_vision_usage(fields, cfg, completion)

    text = completion.text
    payload = _parse_payload(text)
    raw_text = str(payload.get("raw_text") or "").strip()
    if not raw_text:
        raise UpstreamError("图片解析失败：模型没有给出热点描述（raw_text 为空）")
    if len(raw_text) > MAX_RAW_TEXT_CHARS:
        raise UpstreamError(
            f"图片解析失败：热点描述超过 {MAX_RAW_TEXT_CHARS} 字上限（{len(raw_text)} 字）"
        )
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
    return ImageClueParse(
        raw_text=raw_text,
        clue=clue,
        prompt_versions={rendered.task_id: rendered.version},
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        cost_cny=_cost_cny(cfg, completion),
        usage_status=str(completion.usage_status or "estimated"),
    )


async def parse_image_clue_multi(
    cfg: AppConfig,
    images: Sequence[tuple[bytes, str]],
    *,
    http: httpx.AsyncClient | None = None,
) -> ImageClueParse:
    """把**一组**图片（1–9 张，按发送顺序）一次多模态调用拆成一份热点描述与一份线索（S7.15）。

    与单图路径同口径：503（未启用 / 缺密钥）、502（上游或契约问题）、`MAX_RAW_TEXT_CHARS`、
    返回同一个 `ImageClueParse`；`prompt_versions` 记 `image_hotspot_clue_multi`。

    **多图专属校验**（不满足即 502，用户点「重解析」就是显式重试）：
    ① 要素 3–8 个；② 每个要素的 `evidence` 非空（prompt 要求写成「图N：观察」）。

    刻意**不复用** `parse_image_clue` 的函数体：S7.15 要求单图路径零改动，因此这一份是独立实现，
    只共用模块级的 `_client_or_stack` / `_parse_payload` / `_cost_cny` 与常量。
    """
    if not images:
        raise ValueError("parse_image_clue_multi 至少要一张图片")
    if not images_ready(cfg):
        raise DependencyUnavailableError(
            "图片热点解析未启用：需要 [vision].enabled = true 与密钥",
            {
                "vision_enabled": cfg.vision.enabled,
                "api_key_env": cfg.vision.api_key_env,
            },
        )

    rendered = prompt_tool.render(MULTI_TASK_ID)
    async with _client_or_stack(cfg, http) as client:
        api = build_image_client(cfg, client=client)
        if api is None:  # pragma: no cover - images_ready 已经拦过一遍，这里只是兜底
            raise DependencyUnavailableError(
                "图片热点解析未启用：缺少可用的多模态客户端"
            )
        with _vision_generation(
            cfg,
            task_id=rendered.task_id,
            version=rendered.version,
            image_count=len(images),
        ) as fields:
            try:
                completion = await api.complete_many(rendered, images)
            except VisionError as exc:
                fields.level = LEVEL_ERROR
                fields.status_message = "llm_error"
                fields.metadata["usage_status"] = "unknown"
                raise UpstreamError(
                    f"图片解析失败：{exc}", {"detail": str(exc)[:200]}
                ) from exc
            _fill_vision_usage(fields, cfg, completion)

    payload = _parse_payload(completion.text)
    raw_text = str(payload.get("raw_text") or "").strip()
    if not raw_text:
        raise UpstreamError("图片解析失败：模型没有给出热点描述（raw_text 为空）")
    if len(raw_text) > MAX_RAW_TEXT_CHARS:
        raise UpstreamError(
            f"图片解析失败：热点描述超过 {MAX_RAW_TEXT_CHARS} 字上限（{len(raw_text)} 字）"
        )
    clue_payload = payload.get("clue")
    if not isinstance(clue_payload, dict):
        raise UpstreamError("图片解析失败：返回里缺少 clue 对象")
    try:
        clue = HotspotClue.from_dict(clue_payload, hotspot_raw=raw_text)
    except SchemaError as exc:
        raise UpstreamError(f"图片解析失败：线索结构不合契约（{exc}）") from exc
    _check_multi_clue(clue)
    return ImageClueParse(
        raw_text=raw_text,
        clue=clue,
        prompt_versions={rendered.task_id: rendered.version},
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        cost_cny=_cost_cny(cfg, completion),
        usage_status=str(completion.usage_status or "estimated"),
    )


def _check_multi_clue(clue: HotspotClue) -> None:
    """多图专属校验（S7.15）：要素 3–8 个，且每个都必须带非空 `evidence`（指图）。"""
    count = len(clue.elements)
    if not (MULTI_MIN_ELEMENTS <= count <= MULTI_MAX_ELEMENTS):
        raise UpstreamError(
            f"多图解析失败：要素数量必须是 {MULTI_MIN_ELEMENTS}–{MULTI_MAX_ELEMENTS} 个"
            f"（实际 {count} 个）"
        )
    for index, element in enumerate(clue.elements, start=1):
        if not str(element.evidence or "").strip():
            raise UpstreamError(
                f"多图解析失败：第 {index} 个要素缺少 evidence（必须指明来自哪一张图）"
            )


def _parse_payload(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON 对象；不是对象就按上游失败处理。"""
    try:
        parsed = extract_json(text)
    except ValueError as exc:
        raise UpstreamError(f"图片解析失败：返回不是 JSON（{exc}）") from exc
    if not isinstance(parsed, dict):
        raise UpstreamError("图片解析失败：返回的不是 JSON 对象")
    return parsed
