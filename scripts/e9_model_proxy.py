"""启动 E9 本地模型转发服务（只允许冻结模型与路径）。

配置 JSON（不含密钥）：

{
  "upstream_base_url": "https://api.example.com/v1",
  "local_api_key": "给业务子进程的临时 key",
  "budget_cny": 10,
  "ledger": "runs/_e9/budget/ledger.json",
  "prices": {"source": "...", "verified_at": "...", "models": {...}}
}

真实上游密钥只从 `E9_UPSTREAM_API_KEY` 环境变量读取；不写入配置、日志或提交。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import uvicorn

from xhs_agent.evaluation.budget import BudgetLedger, PriceTable
from xhs_agent.evaluation.proxy import ProxyConfig, create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E9 本地模型转发与费用守卫")
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args(argv)
    data = json.loads(Path(args.config).read_text(encoding="utf-8"))
    key = os.environ.get("E9_UPSTREAM_API_KEY", "")
    if not key:
        print("E9_UPSTREAM_API_KEY 为空：拒绝启动，避免无凭据伪造成功", file=sys.stderr)
        return 3
    prices = PriceTable.from_mapping(data["prices"])
    ledger = BudgetLedger(Path(data["ledger"]), budget_cny=float(data.get("budget_cny", 10)),
                          prices=prices)
    config = ProxyConfig(
        upstream_base_url=str(data["upstream_base_url"]),
        upstream_api_key=key,
        local_api_key=str(data.get("local_api_key", "")),
        timeout_s=float(data.get("timeout_s", 120)),
    )
    app = create_app(config, ledger, prices)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
