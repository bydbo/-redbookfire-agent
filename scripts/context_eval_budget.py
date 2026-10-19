"""评测专用的累计预算与传输边界；不导入产品代码，可被不同代码基线共用。"""
from __future__ import annotations

import gzip
import json
import math
import zlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


class BudgetExceeded(RuntimeError):
    pass


class SpendLedger:
    """独占持久账本；未知预留跨重启保留，所有阶段共用原累计额度。"""

    def __init__(self, path: Path, limit: float) -> None:
        if not 0 < limit <= 10:
            raise ValueError("本次授权额度必须在 0～10 元之间")
        self.path, self.limit = path.resolve(), limit
        self.lock = self.path.with_suffix(".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.handle = self.lock.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise ValueError("预算锁已存在；先核实原进程已终止，不能并行重置预算") from exc
        try:
            self.handle.write(datetime.now(UTC).isoformat())
            self.handle.flush()
            self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
                "limit_cny": limit, "records": []}
            self.limit = min(limit, float(self.data["limit_cny"]))
            for record in self.data["records"]:
                if not math.isfinite(float(record["charged_upper_cny"])) or record["charged_upper_cny"] < 0:
                    raise ValueError("费用账本包含非法记录，停止发送")
            self.save()
        except BaseException:
            self.close()
            raise

    @property
    def committed(self) -> float:
        return sum(float(row["charged_upper_cny"]) for row in self.data["records"])

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def reserve(self, label: str, upper: float) -> int:
        if not math.isfinite(upper) or upper < 0 or self.committed + upper > self.limit:
            raise BudgetExceeded(f"预算不足：已占用 {self.committed:.4f} 元，本次需预留 {upper:.4f} 元")
        self.data["records"].append({"label": label, "reserved_upper_cny": upper,
            "charged_upper_cny": upper, "state": "unknown_reserved", "at": datetime.now(UTC).isoformat()})
        self.save()
        return len(self.data["records"]) - 1

    def settle(self, index: int, *, upper: float, usage_status: str) -> None:
        row = self.data["records"][index]
        if usage_status == "actual":
            if not math.isfinite(upper) or upper < 0:
                raise BudgetExceeded("实际费用数据非法，停止后续调用")
            row.update(charged_upper_cny=upper, state="known_usage_upper")
            self.save()
            if upper > row["reserved_upper_cny"]:
                raise BudgetExceeded("实际用量超过保守预留，已保留实际估算并停止后续调用")

    def close(self) -> None:
        self.handle.close()
        self.lock.unlink(missing_ok=True)


def response_usage(raw: bytes, *, streaming: bool, content_encoding: str = "") -> dict[str, Any]:
    """只读取用量，不把供应商原文或密钥写入累计账本。"""
    try:
        if content_encoding.lower() == "gzip":
            raw = gzip.decompress(raw)
        elif content_encoding.lower() == "deflate":
            raw = zlib.decompress(raw)
    except (OSError, EOFError, zlib.error):
        return {}
    candidates = [raw.decode("utf-8", errors="replace")]
    if streaming:
        candidates = [line[5:].strip() for line in candidates[0].splitlines() if line.startswith("data:")]
    usage: dict[str, Any] = {}
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("usage"), dict):
            usage = value["usage"]
    return usage


class _BudgetStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, finished: Any) -> None:
        self.stream, self.finished = stream, finished
        self.parts: list[bytes] = []
        self.done = False

    def settle(self) -> None:
        if not self.done:
            self.done = True
            self.finished(b"".join(self.parts))

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for part in self.stream:
                self.parts.append(part)
                yield part
        finally:
            self.settle()

    async def aclose(self) -> None:
        try:
            await self.stream.aclose()
        finally:
            self.settle()


class BudgetTransport(httpx.AsyncBaseTransport):
    """每次真实发送前预留；非流式与 SSE 均按供应商已知 usage 结算，不改响应时序。"""

    def __init__(self, ledger: SpendLedger, cfg: Any, *, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self.ledger, self.cfg = ledger, cfg
        self.inner = inner if inner is not None else httpx.AsyncHTTPTransport()
        self.last: list[dict[str, Any]] = []
        self.stopped = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model = str(body.get("model") or "")
        price_cfg = self.cfg.vision if model == self.cfg.vision.model else self.cfg.llm
        in_price = max(float(price_cfg.price_in_per_m) * 2, 6.0)
        out_price = max(float(price_cfg.price_out_per_m) * 2, 20.0)
        upper = ((len(request.content) + 4096) * in_price + int(body.get("max_tokens") or 16000) * out_price) / 1_000_000
        try:
            index = self.ledger.reserve(f"{request.url.host}:{model}", upper)
        except BudgetExceeded:
            self.stopped = True
            raise
        row: dict[str, Any] = {"model": model, "budget_record": index, "usage": {}, "state": "started"}
        self.last.append(row)

        def finished(raw: bytes) -> None:
            usage = response_usage(raw, streaming=bool(body.get("stream")),
                                   content_encoding=row.get("content_encoding", ""))
            row.update(usage=usage, state="finished")
            incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if type(incoming) is int and type(outgoing) is int and min(incoming, outgoing) >= 0:
                try:
                    self.ledger.settle(index, upper=(incoming * in_price + outgoing * out_price) / 1_000_000,
                                       usage_status="actual")
                except BudgetExceeded:
                    self.stopped = True
                    raise

        try:
            response = await self.inner.handle_async_request(request)
        except BaseException as exc:
            row.update(state="unknown_reserved", error=type(exc).__name__)
            raise
        row["http_status"] = response.status_code
        row["content_encoding"] = response.headers.get("content-encoding", "")
        return httpx.Response(response.status_code, headers=response.headers,
            stream=_BudgetStream(response.stream, finished), extensions=response.extensions)

    async def aclose(self) -> None:
        await self.inner.aclose()
