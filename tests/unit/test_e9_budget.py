"""E9 费用守卫：预留、结算、缺 usage、重启与跨进程竞争。"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from xhs_agent.evaluation.budget import (
    BudgetError,
    BudgetLedger,
    PriceTable,
    price_table_from_config,
)


def _reserve_worker(args: tuple[str, float]) -> bool:
    path, amount = args
    try:
        BudgetLedger(path, budget_cny=10.0).reserve(
            f"req-{multiprocessing.current_process().pid}-{amount}", "text-model", "text",
            amount=amount)
        return True
    except BudgetError:
        return False


def test_reserve_and_settle(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "ledger.json", budget_cny=10.0)
    ledger.reserve("req-1", "text-model", "text", amount=2.0)
    usage = ledger.settle("req-1", 0.5, {"prompt_tokens": 100})
    assert usage.status == "settled"
    summary = ledger.summary()
    assert summary["budget_cny"] == 10.0
    assert summary["settled_cny"] == 0.5
    assert summary["reserved_cny"] == 0.0
    assert summary["available_cny"] == 9.5


def test_over_budget_is_rejected_before_send(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "ledger.json", budget_cny=1.0)
    with pytest.raises(BudgetError, match="预算不足"):
        ledger.reserve("req-1", "text-model", "text", amount=1.01)
    assert ledger.summary()["requests"] == 0


def test_duplicate_request_is_rejected(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "ledger.json", budget_cny=10.0)
    ledger.reserve("req-1", "text-model", "text", amount=1.0)
    with pytest.raises(BudgetError, match="重复"):
        ledger.reserve("req-1", "text-model", "text", amount=1.0)


def test_unknown_usage_is_kept_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = BudgetLedger(path, budget_cny=10.0)
    ledger.reserve("req-1", "text-model", "text", amount=1.5)
    ledger.mark_unknown("req-1", "stream disconnected")
    reopened = BudgetLedger(path, budget_cny=10.0)
    summary = reopened.summary()
    assert summary["unknown_cny"] == 1.5
    assert summary["reserved_cny"] == 0.0
    assert summary["available_cny"] == 8.5


def test_new_budget_value_cannot_reset_existing_ledger(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    BudgetLedger(path, budget_cny=10.0)
    with pytest.raises(BudgetError, match="预算"):
        BudgetLedger(path, budget_cny=20.0)


def test_missing_model_price_blocks_request(tmp_path: Path) -> None:
    table = PriceTable(models={})
    ledger = BudgetLedger(tmp_path / "ledger.json", budget_cny=10.0, prices=table)
    with pytest.raises(BudgetError, match="价格"):
        ledger.reserve("req-1", "unknown-model", "text")


def test_price_table_from_config_uses_configured_prices() -> None:
    class Cfg:
        class llm:
            price_in_per_m = 2.0
            price_out_per_m = 8.0
            max_tokens = 1000
            model = "m"

        class vision:
            price_in_per_m = 3.0
            price_out_per_m = 9.0
            max_tokens = 1000
            model = "v"

        class embedding:
            price_in_per_m = 0.5
            model = "e"

    table = price_table_from_config(Cfg())  # type: ignore[arg-type]
    assert table.reserve("m", "text") == pytest.approx(10_000 / 1_000_000)
    assert table.reserve("e", "embedding") == pytest.approx(4_000 / 1_000_000)


def test_ledger_is_persisted_as_readable_json(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = BudgetLedger(path, budget_cny=10.0)
    ledger.reserve("req-1", "text-model", "text", amount=1.0)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["budget_cny"] == 10.0
    assert data["requests"][0]["status"] == "reserved"


def test_concurrent_processes_never_exceed_budget(tmp_path: Path) -> None:
    path = str(tmp_path / "ledger.json")
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(4) as pool:
        results = pool.map(_reserve_worker, [(path, 3.0)] * 4)
    assert sum(results) == 3
    summary = BudgetLedger(tmp_path / "ledger.json", budget_cny=10.0).summary()
    assert summary["reserved_cny"] == 9.0
