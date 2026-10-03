"""文本向量化：把素材文本批量转成 embedding 向量。

用途：给 `materials.embedding` 回填语义向量，供向量召回通道使用；一个工具只做「文本 → 向量」。
输入：`EmbeddingConfig`（端点 / 模型 / 维度 / 批量 / 超时 / 重试 / 密钥）+ 一批文本；
      调用方注入的 `httpx.AsyncClient`（连接池；一次运行共享一个，本模块不自持）。
输出：`EmbeddingResult`（向量列表 / 模型名 / prompt_tokens / 耗时 / 尝试次数）；文本口径见
      `material_embedding_text`。
边界：不做降级——调用在重试后仍失败抛 `EmbeddingError`；返回维度与 `cfg.dim` 不符也抛错
（换模型必须补 ADR 并重建向量，见《检索契约》§九）。
     重试口径不变（`max_retries + 1` 次、退避见 `tools/http.py`、400/401/403 不重试）。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import AppConfig, EmbeddingConfig
from .http import backoff_seconds

# 单请求批量上限：DashScope text-embedding-v3 兼容端点实测硬上限为 10
# （11 条即 400 "batch size is invalid, it should not be larger than 10"）。
# 换供应商必须复核这里，并同步《检索契约》§三 的说明。
MAX_BATCH_PER_REQUEST = 10


class EmbeddingError(RuntimeError):
    """向量化调用失败，或返回值与契约不符。"""


@dataclass
class EmbeddingResult:
    """一次（可能跨多个请求的）向量化结果。"""

    vectors: list[list[float]]
    model: str = ""
    prompt_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 0


def material_embedding_text(title: str, description: str, tags: list[str] | None,
                            elements: list[Any] | None) -> str:
    """素材的向量化文本：**标签 → 标题 → 描述 → 要素值**（去重、去空、空格连接）。

    输入：四个取自素材的字段；`elements` 兼容数据库里的 JSONB `dict` 与 `schemas.Element`。
    输出：拼接后的字符串；四部分全空时返回 `""`——调用方应跳过该行，不浪费一次 API 调用。
    """
    parts: list[str] = []
    for tag in tags or []:
        _push(parts, tag)
    _push(parts, title)
    _push(parts, description)
    for element in elements or []:
        value = element.get("value") if isinstance(element, dict) else getattr(element, "value", "")
        _push(parts, value)
    return " ".join(parts)


def _push(parts: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in parts:
        parts.append(text)


def chunk_texts(texts: list[str], size: int) -> list[list[str]]:
    """把文本按 `size` 切片（`size < 1` 按 1 处理）。"""
    step = max(1, int(size))
    return [texts[i:i + step] for i in range(0, len(texts), step)]


class EmbeddingClient:
    """OpenAI 兼容 `/embeddings` 客户端：批量调用 + 重试 + 维度校验。"""

    def __init__(self, cfg: EmbeddingConfig, *, client: httpx.AsyncClient) -> None:
        self.cfg = cfg
        self.base_url = (cfg.base_url or "").rstrip("/")
        if not self.base_url:
            raise EmbeddingError("缺少 base_url，无法调用向量服务")
        self.api_key = cfg.resolved_key()
        self.model = cfg.model
        self.batch_size = max(1, min(int(cfg.batch_size), MAX_BATCH_PER_REQUEST))
        self.client = client

    @property
    def label(self) -> str:
        return f"embedding:{self.model}"

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """把 `texts` 全部向量化，返回值与入参一一对应（顺序一致）。

        内部按 `min(batch_size, MAX_BATCH_PER_REQUEST)` 切分请求；每批独立重试。
        """
        if not texts:
            return EmbeddingResult(vectors=[], model=self.model)
        vectors: list[list[float]] = []
        prompt_tokens = 0
        attempts = 0
        started = time.time()
        for batch in chunk_texts(list(texts), self.batch_size):
            batch_vectors, batch_tokens, batch_attempts = await self._embed_batch(batch)
            vectors.extend(batch_vectors)
            prompt_tokens += batch_tokens
            attempts += batch_attempts
        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            prompt_tokens=prompt_tokens,
            latency_ms=int((time.time() - started) * 1000),
            attempts=attempts,
        )

    async def _embed_batch(self, batch: list[str]) -> tuple[list[list[float]], int, int]:
        """单个请求：失败按 `max_retries` 退避重试；400/401/403 与结构错误不重试。"""
        url = f"{self.base_url}/embeddings"
        payload = {"model": self.model, "input": batch}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        last_error = ""
        plain_attempts = 0
        for attempt in range(1, self.cfg.max_retries + 2):
            plain_attempts = attempt
            try:
                response = await self.client.post(url, json=payload, headers=headers,
                                                  timeout=self.cfg.timeout_s)
                if response.status_code >= 400:
                    detail = response.text[:300]
                    last_error = (f"HTTP {response.status_code}: "
                                  f"{detail or response.reason_phrase}")
                    if response.status_code in (400, 401, 403):
                        break
                else:
                    data = response.json()
                    vectors = self._parse(data, len(batch))
                    usage = data.get("usage") if isinstance(data, dict) else None
                    return vectors, int((usage or {}).get("prompt_tokens") or 0), attempt
            except EmbeddingError as exc:
                # 返回结构或维度不符：重试拿不到不同结果，直接失败
                last_error = str(exc)
                break
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt <= self.cfg.max_retries:
                await asyncio.sleep(backoff_seconds(attempt))
        raise EmbeddingError(f"向量化失败（已尝试 {plain_attempts} 次）：{last_error or '调用失败'}")

    def _parse(self, data: Any, expected: int) -> list[list[float]]:
        """校验并解析响应：条数、顺序（按 `index`）、维度都要对。"""
        try:
            items = data["data"]
        except (KeyError, TypeError) as exc:
            raise EmbeddingError(f"返回结构异常：{str(data)[:200]}") from exc
        if not isinstance(items, list) or len(items) != expected:
            actual = len(items) if isinstance(items, list) else "非列表"
            raise EmbeddingError(f"返回向量条数不符：期望 {expected}，实际 {actual}")
        ordered = sorted(items, key=lambda item: int(item.get("index") or 0))
        vectors: list[list[float]] = []
        for item in ordered:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError("返回的 embedding 不是非空数组")
            if len(vector) != self.cfg.dim:
                raise EmbeddingError(
                    f"向量维度 {len(vector)} 与契约维度 {self.cfg.dim} 不符："
                    "换 embedding 模型必须先补 ADR、修订数据契约与检索契约，再重建向量"
                    "（《检索契约》§九）")
            vectors.append([float(value) for value in vector])
        return vectors


def build_embedder(cfg: AppConfig, *, client: httpx.AsyncClient) -> EmbeddingClient | None:
    """按配置造客户端。

    `[embedding].enabled = false` → 返回 `None`（能力未启用，属能力裁剪）；
    已启用但密钥为空 → 抛 `EmbeddingError`（不降级）。
    """
    if not cfg.embedding.enabled:
        return None
    if not cfg.embedding.resolved_key():
        raise EmbeddingError(
            f"[embedding].enabled = true 但密钥 {cfg.embedding.api_key_env} 为空："
            "把密钥写进 config/.env（模板见 config/.env.example），或导出同名环境变量")
    return EmbeddingClient(cfg.embedding, client=client)
