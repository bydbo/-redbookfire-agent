"""运维接口（openapi tag：运维）：`GET /api/health`。

用法：逐项探测 db / redis / llm，任一不可用即整体 `down` 并返回 503。
输入：无（走 `get_config()`；db 用进程级引擎，redis 读 `REDIS_URL`）。
输出：契约 `Health` 形状的 `{"status", "checks": {"db", "redis", "llm"}}`；
      **无论依赖好坏都返回这个形状**（不能退化成 ErrorResponse），否则契约的 503 分支对不上。

口径：llm 项**只查配置、不发真实请求**——真实探测会烧钱，也容易被网络抖动误判
（与 `probe.py`「模型服务不在启动时探测」的既有口径一致）。
"""

from __future__ import annotations

import time
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ...config import AppConfig
from ..deps import ConfigDep, get_engine, request_id_header
from ..models import Health

router = APIRouter(tags=["运维"], dependencies=[Depends(request_id_header)])

_PROBE_TIMEOUT_S = 2.0


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _down(detail: str, started: float | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "down", "detail": detail[:200]}
    if started is not None:
        payload["latency_ms"] = _ms(started)
    return payload


async def check_database() -> dict[str, Any]:
    """数据库探活：拿进程级引擎跑一次 `SELECT 1`。"""
    started = time.perf_counter()
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:   # 未配置 DSN / 连不上 / 查询失败都算 down
        return _down(f"{type(exc).__name__}: {exc}", started)
    return {"status": "ok", "latency_ms": _ms(started)}


async def check_redis(cfg: AppConfig) -> dict[str, Any]:
    """Redis 探活：`REDIS_URL` 未配置即 down（P3 起才必填，这里如实反映）。"""
    url = (cfg.env_view.get("REDIS_URL") or "").strip()
    if not url:
        return _down("REDIS_URL 未配置（配置契约 §2.1.1：P3 起必填）")
    started = time.perf_counter()
    client = aioredis.from_url(url, socket_connect_timeout=_PROBE_TIMEOUT_S,
                               socket_timeout=_PROBE_TIMEOUT_S)
    try:
        await client.ping()
    except Exception as exc:
        return _down(f"{type(exc).__name__}: {exc}", started)
    finally:
        await client.aclose()
    return {"status": "ok", "latency_ms": _ms(started)}


def check_llm(cfg: AppConfig) -> dict[str, Any]:
    """文本模型探活：只确认 base_url 与密钥都在（不发请求）。"""
    if not cfg.llm.base_url:
        return _down("文本模型 base_url 未配置")
    if not cfg.llm.resolved_key():
        return _down(f"文本模型密钥为空：{cfg.llm.api_key_env}")
    return {"status": "ok", "detail": f"{cfg.llm.provider} / {cfg.llm.model}"}


@router.get("/health", response_model=Health,
            responses={503: {"model": Health, "description": "存在不可用依赖"}})
async def get_health(cfg: ConfigDep) -> JSONResponse:
    """健康检查：三项都 ok 返回 200，否则 503（响应体形状不变）。"""
    checks = {
        "db": await check_database(),
        "redis": await check_redis(cfg),
        "llm": check_llm(cfg),
    }
    healthy = all(item["status"] == "ok" for item in checks.values())
    return JSONResponse(status_code=200 if healthy else 503,
                        content={"status": "ok" if healthy else "down", "checks": checks})
