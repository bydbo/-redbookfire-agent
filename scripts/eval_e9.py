"""E9 评测 CLI（薄入口；逻辑在 `xhs_agent.evaluation.cli`）。

用法见 `docs/E9评测协议.md` 与计划第五节。默认不发起外部模型请求。
"""

from __future__ import annotations

from xhs_agent.evaluation.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
