"""独立冻结评测与预算上限：不调用真实端点。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def runner():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("context_eval_under_test", ROOT / "scripts/eval_context_repair.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_frozen_inputs_and_plan_counts(runner):
    suite = runner.validate_assets()
    assert [case["id"] for case in suite["cases"]] == [f"T{i:02}" for i in range(1, 13)]
    assert sum(len(case["turns"]) for case in suite["cases"]) == 108
    assert all(isinstance(turn, str) and turn for case in suite["cases"] for turn in case["turns"])


def test_session_runner_keeps_stops_in_denominator_and_requires_exact_evidence():
    import copy

    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("session_eval_under_test", ROOT / "scripts/eval_context_sessions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = {"sessions": [{"case_id": "T09", "session_id": "s1", "protocol_probes": [], "turns": [
        {"number": 4, "status": "failed", "http_status": 200, "new_transport_requests": 0,
         "error_category": "required_context_overflow", "context": {"facts": list(range(41))}},
    ]}]}
    assert module.execution_failures(source) == []
    assert len(source["sessions"][0]["turns"]) == 1
    assert source["sessions"][0]["turns"][0]["status"] == "failed"
    for field, value in (("new_transport_requests", 1), ("error_category", "schema_error"),
                         ("context", {"facts": []})):
        bad = copy.deepcopy(source)
        bad["sessions"][0]["turns"][0][field] = value
        assert module.execution_failures(bad) == [{"case_id": "T09", "turn": 4, "reason": "expected_stop_missing"}]


def test_session_runner_does_not_accept_an_unrelated_404():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("session_eval_404", ROOT / "scripts/eval_context_sessions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = {"sessions": [{"case_id": "T08", "session_id": "s1", "protocol_probes": [],
        "fixture_objects": [{"run_id": "foreign", "owner_session": "s2"}], "turns": [
            {"number": 4, "http_status": 404, "new_transport_requests": 0, "http_error": {"detail": "Not Found"}}]}]}
    assert module.execution_failures(report) == [{"case_id": "T08", "turn": 4, "reason": "expected_stop_missing"}]
    report["sessions"][0]["turns"][0]["http_error"] = {"code": "not_found", "detail": {"run_id": "foreign"}}
    assert module.execution_failures(report) == []


def test_budget_cannot_exceed_cap_or_reset_after_restart(runner, tmp_path):
    path = tmp_path / "ledger.json"
    ledger = runner.SpendLedger(path, 10)
    ledger.reserve("unknown", 7)
    with pytest.raises(runner.BudgetExceeded):
        ledger.reserve("blocked", 4)
    ledger.close()
    resumed = runner.SpendLedger(path, 10)
    assert resumed.committed == 7
    with pytest.raises(runner.BudgetExceeded):
        resumed.reserve("blocked", 4)
    resumed.close()


def test_known_usage_settles_and_parallel_lock_prevents_overspend(runner, tmp_path):
    path = tmp_path / "ledger.json"
    ledger = runner.SpendLedger(path, 2)
    index = ledger.reserve("known", 1)
    ledger.settle(index, upper=0.2, usage_status="actual")
    assert ledger.committed == 0.2
    with pytest.raises(ValueError, match="预算锁"):
        runner.SpendLedger(path, 10)
    ledger.close()
    with pytest.raises(ValueError):
        runner.SpendLedger(path, 11)


@pytest.mark.asyncio
async def test_live_runner_records_parsed_delta_without_real_calls(runner, tmp_path, monkeypatch):
    import json
    from argparse import Namespace

    from xhs_agent.config import AppConfig
    from xhs_agent.tools.llm import BaseProvider, LLMResult

    class FakeProvider(BaseProvider):
        async def complete(self, _call):
            return LLMResult(text='{"operations":[],"proposals":[]}', model="local")

    monkeypatch.setattr(runner, "load_config", AppConfig)
    monkeypatch.setattr(runner, "build_provider", lambda *_args, **_kwargs: FakeProvider())
    args = Namespace(ledger=str(tmp_path / "ledger.json"), budget_cny=10,
                     out_dir=str(tmp_path), phase="extract", prefix_groups=0)
    assert await runner.live(args, runner.validate_assets()) == 0
    results = json.loads((tmp_path / "model-results.json").read_text(encoding="utf-8"))
    assert len(results["results"]) == 8
    assert all(row["delta"] == {"operations": [], "proposals": []} for row in results["results"])


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("compressed", [False, True])
async def test_budget_transport_settles_usage_without_buffering_stream(runner, tmp_path, streaming, compressed):
    import gzip

    import httpx

    from xhs_agent.config import AppConfig

    usage = {"prompt_tokens": 100, "completion_tokens": 20}
    payload = '{"usage":{"prompt_tokens":100,"completion_tokens":20}}'
    raw = (f"data: {payload}\n\ndata: [DONE]\n\n" if streaming else payload).encode()
    transport = httpx.MockTransport(lambda _request: httpx.Response(200,
        content=gzip.compress(raw) if compressed else raw,
        headers={"content-encoding": "gzip"} if compressed else {}))
    ledger = runner.SpendLedger(tmp_path / "ledger.json", 10)
    bounded = runner.BudgetTransport(ledger, AppConfig(), inner=transport)
    async with httpx.AsyncClient(transport=bounded) as client:
        response = await client.post("https://local.test/v1/chat/completions",
            json={"model": "text", "stream": streaming, "max_tokens": 100})
        assert response.status_code == 200
    assert bounded.last[0]["usage"] == usage
    assert ledger.data["records"][0]["state"] == "known_usage_upper"
    assert ledger.committed == pytest.approx((100 * 6 + 20 * 20) / 1_000_000)
    ledger.close()


@pytest.mark.asyncio
async def test_budget_prevents_actual_send_and_preserves_unknown_on_error(runner, tmp_path):
    import httpx

    from xhs_agent.config import AppConfig

    count = 0

    def transport(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("timeout", request=request)

    ledger = runner.SpendLedger(tmp_path / "ledger.json", 0.05)
    bounded = runner.BudgetTransport(ledger, AppConfig(), inner=httpx.MockTransport(transport))
    async with httpx.AsyncClient(transport=bounded) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.post("https://local.test/v1", json={"model": "text", "max_tokens": 100})
        reserved = ledger.committed
        assert reserved > 0 and ledger.data["records"][0]["state"] == "unknown_reserved"
        with pytest.raises(runner.BudgetExceeded):
            await client.post("https://local.test/v1", json={"model": "text", "max_tokens": 1000})
    assert count == 1 and ledger.committed == reserved and bounded.stopped
    ledger.close()
