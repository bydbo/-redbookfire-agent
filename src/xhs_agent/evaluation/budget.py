"""E9 统一费用守卫：本地持久账本 + 发送前预留。

设计边界（协议 §5.3 / §5.4）：
- 单进程 / 多客户端通过文件锁串行化；账本原子落盘，换输出目录不能重置预算；
- 每次实际发送前先 `reserve`，超预算直接拒绝，调用方不得再发；
- 缺 usage、断流或进程退出后调用 `mark_unknown`，预留不自动释放；
- 供应商价格必须来自带日期的快照；未知模型没有价格就阻断，不猜。
"""

from __future__ import annotations

import importlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import RequestUsage

# 平台模块动态导入：Windows 用 msvcrt，POSIX 用 fcntl；mypy 在 Windows 上也能通过。
_lock: Any = importlib.import_module("msvcrt" if os.name == "nt" else "fcntl")


class BudgetError(RuntimeError):
    """预算、价格或账本状态不允许本次请求。"""


@dataclass(frozen=True)
class Price:
    """一个模型的保守价格上界（人民币元 / 百万 token）。"""

    input_per_m: float
    output_per_m: float
    max_input_tokens: int
    max_output_tokens: int

    def reserve(self, kind: str) -> float:
        if kind == "embedding":
            amount = self.max_input_tokens * self.input_per_m / 1_000_000
        elif kind in ("text", "vision"):
            amount = (self.max_input_tokens * self.input_per_m
                      + self.max_output_tokens * self.output_per_m) / 1_000_000
        else:
            raise BudgetError(f"未知请求类型：{kind}")
        return round(amount, 8)


@dataclass
class PriceTable:
    """模型白名单与价格上界；`source` 只作审计，不参与计算。"""

    models: dict[str, Price] = field(default_factory=dict)
    source: str = ""
    verified_at: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> PriceTable:
        models: dict[str, Price] = {}
        for name, item in dict(data.get("models") or {}).items():
            try:
                models[str(name)] = Price(
                    input_per_m=float(item["input_per_m"]),
                    output_per_m=float(item["output_per_m"]),
                    max_input_tokens=int(item["max_input_tokens"]),
                    max_output_tokens=int(item["max_output_tokens"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise BudgetError(f"价格表条目无效：{name}: {exc}") from exc
        return cls(models=models, source=str(data.get("source") or ""),
                   verified_at=str(data.get("verified_at") or ""))

    def reserve(self, model: str, kind: str) -> float:
        price = self.models.get(model)
        if price is None:
            raise BudgetError(f"模型 {model!r} 没有价格快照，阻断请求")
        return price.reserve(kind)


def price_table_from_config(cfg: Any) -> PriceTable:
    """从现有配置构造**未验证**的保守价格表；live 前必须换成官方价格快照。"""
    llm = cfg.llm
    vision = cfg.vision
    embedding = cfg.embedding
    return PriceTable(models={
        str(llm.model): Price(
            input_per_m=float(llm.price_in_per_m),
            output_per_m=float(llm.price_out_per_m),
            max_input_tokens=int(getattr(llm, "max_tokens", 16000)),
            max_output_tokens=int(getattr(llm, "max_tokens", 16000)),
        ),
        str(vision.model): Price(
            input_per_m=float(vision.price_in_per_m),
            output_per_m=float(vision.price_out_per_m),
            max_input_tokens=int(getattr(vision, "max_tokens", 16000)),
            max_output_tokens=int(getattr(vision, "max_tokens", 16000)),
        ),
        str(embedding.model): Price(
            input_per_m=float(embedding.price_in_per_m),
            output_per_m=0.0,
            max_input_tokens=8000,
            max_output_tokens=0,
        ),
    }, source="config-default-unverified", verified_at="")

    # 注：config 的 llm/vision 目前没有 max_tokens；保留 getattr 让价格表可复用。


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class BudgetLedger:
    """一个预算账本文件；所有读写都在同一把跨进程文件锁内完成。"""

    VERSION = 1

    def __init__(self, path: Path, *, budget_cny: float = 10.0,
                 prices: PriceTable | None = None) -> None:
        if budget_cny <= 0:
            raise BudgetError("预算必须大于 0")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prices = prices if prices is not None else PriceTable(models={})
        with self._lock():
            data = self._read()
            if data is None:
                data = {"version": self.VERSION, "budget_cny": round(budget_cny, 8),
                        "created_at": _now(), "updated_at": _now(), "requests": []}
                self._write(data)
            elif abs(float(data["budget_cny"]) - budget_cny) > 1e-9:
                raise BudgetError(
                    f"既有账本预算 {data['budget_cny']} 与本次 {budget_cny} 不一致；"
                    "不能通过换目录或改参数重置预算")
        self._data: dict[str, Any] = data

    @contextmanager
    def _lock(self) -> Iterator[None]:
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            if os.name == "nt":
                handle.seek(0)
                handle.write(b"\0")
                handle.flush()
                handle.seek(0)
                _lock.locking(handle.fileno(), _lock.LK_LOCK, 1)
            else:  # pragma: no cover
                _lock.flock(handle.fileno(), _lock.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    _lock.locking(handle.fileno(), _lock.LK_UNLCK, 1)
                else:  # pragma: no cover
                    _lock.flock(handle.fileno(), _lock.LOCK_UN)
            finally:
                handle.close()

    def _read(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BudgetError(f"账本损坏：{exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("requests"), list):
            raise BudgetError("账本结构无效")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        data["updated_at"] = _now()
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _refresh(self) -> dict[str, Any]:
        data = self._read()
        if data is None:
            raise BudgetError("账本不存在")
        self._data = data
        return data

    def reserve(self, request_id: str, model: str, kind: str, *,
                amount: float | None = None, attempt_id: str = "",
                upstream: str = "") -> float:
        """预留费用；返回实际预留额。预算不足或重复 request_id 时不落账。"""
        with self._lock():
            data = self._refresh()
            requests = data["requests"]
            if any(item.get("request_id") == request_id for item in requests):
                raise BudgetError(f"重复 request_id：{request_id}")
            reserved = amount if amount is not None else self.prices.reserve(model, kind)
            reserved = round(float(reserved), 8)
            if reserved < 0:
                raise BudgetError("预留费用不能为负")
            committed = sum(
                float(item.get("estimated_cny", 0)) if item["status"] == "settled"
                else float(item.get("unknown_cny", 0)) if item["status"] == "unknown"
                else 0.0
                for item in requests)
            in_flight = sum(float(item.get("reserved_cny", 0))
                            for item in requests if item["status"] == "reserved")
            available = round(float(data["budget_cny"]) - committed - in_flight, 8)
            if reserved > available + 1e-9:
                raise BudgetError(
                    f"预算不足：需要 {reserved:.8f}，可用 {max(0.0, available):.8f}")
            requests.append({
                "request_id": request_id, "model": model, "kind": kind,
                "attempt_id": attempt_id, "reserved_cny": reserved,
                "status": "reserved", "estimated_cny": 0.0, "unknown_cny": 0.0,
                "upstream": upstream, "usage": {}, "created_at": _now(),
                "updated_at": _now(),
            })
            self._write(data)
            return reserved

    def _find_index(self, data: dict[str, Any], request_id: str) -> int:
        for index, item in enumerate(data["requests"]):
            if item.get("request_id") == request_id:
                return index
        raise BudgetError(f"未知 request_id：{request_id}")

    def _find(self, data: dict[str, Any], request_id: str) -> dict[str, Any]:
        return dict(data["requests"][self._find_index(data, request_id)])

    def settle(self, request_id: str, actual_cny: float,
               usage: dict[str, Any] | None = None) -> RequestUsage:
        """按实际用量结算；实际额高于预留时记录 overrun，不静默吞掉。"""
        actual = round(max(0.0, float(actual_cny)), 8)
        with self._lock():
            data = self._refresh()
            item = data["requests"][self._find_index(data, request_id)]
            if item["status"] != "reserved":
                raise BudgetError(f"请求 {request_id} 状态为 {item['status']}，不能结算")
            reserved = float(item["reserved_cny"])
            payload = dict(usage or {})
            if actual > reserved + 1e-9:
                payload["overrun_cny"] = round(actual - reserved, 8)
            item["status"] = "settled"
            item["estimated_cny"] = actual
            item["reserved_cny"] = 0.0
            item["usage"] = payload
            item["updated_at"] = _now()
            self._write(data)
            return RequestUsage.model_validate(item)

    def mark_unknown(self, request_id: str, reason: str) -> RequestUsage:
        """缺 usage / 断流 / 进程退出：预留额转为未知占用，不释放。"""
        with self._lock():
            data = self._refresh()
            item = data["requests"][self._find_index(data, request_id)]
            if item["status"] != "reserved":
                raise BudgetError(f"请求 {request_id} 状态为 {item['status']}，不能标记未知")
            amount = float(item["reserved_cny"])
            item["status"] = "unknown"
            item["reserved_cny"] = 0.0
            item["unknown_cny"] = amount
            item["usage"] = {"reason": reason}
            item["updated_at"] = _now()
            self._write(data)
            return RequestUsage.model_validate(item)

    def summary(self) -> dict[str, Any]:
        with self._lock():
            data = self._refresh()
        requests = data["requests"]
        settled = round(sum(float(item.get("estimated_cny", 0)) for item in requests
                            if item["status"] == "settled"), 8)
        unknown = round(sum(float(item.get("unknown_cny", 0)) for item in requests
                            if item["status"] == "unknown"), 8)
        reserved = round(sum(float(item.get("reserved_cny", 0)) for item in requests
                             if item["status"] == "reserved"), 8)
        budget = float(data["budget_cny"])
        return {
            "budget_cny": round(budget, 8),
            "settled_cny": settled,
            "unknown_cny": unknown,
            "reserved_cny": reserved,
            "available_cny": round(budget - settled - unknown - reserved, 8),
            "requests": len(requests),
            "status_counts": {status: sum(1 for item in requests if item["status"] == status)
                              for status in ("reserved", "settled", "unknown")},
        }

    def request(self, request_id: str) -> RequestUsage:
        with self._lock():
            data = self._refresh()
        return RequestUsage.model_validate(self._find(data, request_id))
