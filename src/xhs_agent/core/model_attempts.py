"""实际模型传输的旁路生命周期；通过上下文注入持久化回调，不依赖数据库。"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

Observer = Callable[[str, dict[str, Any]], Awaitable[None]]
_observer: ContextVar[Observer | None] = ContextVar("model_attempt_observer", default=None)
_event_loop: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar("attempt_event_loop", default=None)
logger = logging.getLogger("xhs_agent.model_attempts")


def bind(observer: Observer | None) -> tuple[Token[Observer | None], Token[asyncio.AbstractEventLoop | None]]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    return _observer.set(observer), _event_loop.set(loop)


def reset(token: tuple[Token[Observer | None], Token[asyncio.AbstractEventLoop | None]]) -> None:
    _event_loop.reset(token[1])
    _observer.reset(token[0])


@contextmanager
def observe(observer: Observer | None) -> Iterator[None]:
    token = bind(observer)
    try:
        yield
    finally:
        reset(token)


def metadata(cfg: Any, *, kind: str, task: str, endpoint: str,
             prompt_version: int | None = None) -> dict[str, Any]:
    """只固化真实端点的能力 / 配置价格，不读取密钥、不记录请求正文。"""
    url = urlsplit(endpoint)
    safe_endpoint = f"{url.scheme}://{url.hostname or ''}{':' + str(url.port) if url.port else ''}{url.path}"
    return {
        "provider_kind": kind, "task": task, "model": str(cfg.model),
        "endpoint": safe_endpoint,
        "prompt_version": f"{task}:v{prompt_version}" if prompt_version is not None else "",
        "prefix_usage_supported": bool(getattr(cfg, "prefix_usage_supported", False)),
        "stream_usage_supported": bool(getattr(cfg, "stream_usage_supported", False)),
        "thinking_toggle_supported": bool(getattr(cfg, "thinking_toggle_supported", False)),
        "price": {"currency": "CNY", "price_version": "config",
                  "input_per_m": float(cfg.price_in_per_m),
                  "output_per_m": float(cfg.price_out_per_m),
                  "source_status": "config_estimated"},
    }


@dataclass
class Attempt:
    observer: Observer | None
    facts: dict[str, Any]
    started: float
    finished: bool = False
    loop: asyncio.AbstractEventLoop | None = None

    async def emit(self, phase: str, data: dict[str, Any]) -> None:
        if self.observer is None:
            return
        try:
            async with asyncio.timeout(5):
                await self.observer(phase, data)
        except Exception:  # 旁路失败不改业务；缺事件由完整率门槛暴露
            logger.warning("模型尝试观测写入失败：%s", phase, exc_info=True)

    async def finish(self, result: dict[str, Any]) -> None:
        if self.finished:
            return
        self.finished = True
        allowed = ("status", "prompt_tokens", "completion_tokens", "usage_status",
                   "cache_hit_tokens", "cache_miss_tokens", "cache_status", "ttft_ms",
                   "latency_ms", "cost_cny")
        facts = {**self.facts, **{key: result[key] for key in allowed if key in result},
                 "error": bool(result.get("error"))}
        facts.setdefault("latency_ms", int((time.monotonic() - self.started) * 1000))
        await self.emit("finished", facts)

    def finish_sync(self, result: dict[str, Any]) -> None:
        if self.observer is not None and self.loop is not None:
            future = asyncio.run_coroutine_threadsafe(self.finish(result), self.loop)
            try:
                future.result(timeout=6)
            except Exception:
                future.cancel()
                logger.warning("同步视觉终态观测失败", exc_info=True)


async def begin(facts: dict[str, Any], *, identity: str | None = None, attempt: int = 1) -> Attempt:
    handle = Attempt(_observer.get(), {**facts, "identity": identity or str(uuid.uuid4()),
                                      "attempt": attempt}, time.monotonic())
    await handle.emit("started", handle.facts)
    return handle


def begin_sync(facts: dict[str, Any]) -> Attempt:
    """to_thread 中的同步视觉把独立写库交回原事件循环。"""
    loop = _event_loop.get()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    active = _observer.get() if loop is not None and loop.is_running() and running is not loop else None
    handle = Attempt(active, {**facts, "identity": str(uuid.uuid4()), "attempt": 1},
                     time.monotonic(), loop=loop)
    if active is not None and loop is not None:
        future = asyncio.run_coroutine_threadsafe(handle.emit("started", handle.facts), loop)
        try:
            future.result(timeout=6)
        except Exception:
            future.cancel()
            logger.warning("同步视觉开始观测失败", exc_info=True)
    return handle
