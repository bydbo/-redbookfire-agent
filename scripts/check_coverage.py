"""运行单测与真实容器集成测试，保存分项报告，再执行联合覆盖率门槛。

用法：uv run python scripts/check_coverage.py。需要 Docker；不允许跳过失败套件。
只合并本次产生的两个数据文件，避免历史 .coverage 污染验收。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "runs" / "_coverage"


def run(*args: str, env: dict[str, str]) -> int:
    return subprocess.run([sys.executable, "-m", *args], cwd=ROOT, env=env).returncode


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    datasets = []
    for name, marker in (("unit", "not integration"), ("integration", "integration")):
        data = OUTPUT / f".coverage.{name}"
        env = {**os.environ, "COVERAGE_FILE": str(data)}
        # pytest-cov 默认清空所选数据文件；分项不设门槛，但测试失败立即中止。
        result = run("pytest", "-m", marker, "-q", "--cov=xhs_agent",
                     "--cov-fail-under=0", "--cov-report=term",
                     f"--cov-report=json:{OUTPUT / (name + '.json')}", env=env)
        if result:
            return result
        datasets.append(str(data))
    env = {**os.environ, "COVERAGE_FILE": str(OUTPUT / ".coverage.combined")}
    result = run("coverage", "combine", "--keep", *datasets, env=env)
    if result:
        return result
    # 配置中的 fail_under=78 只在联合报告上执行；不使用 append 或忽略门槛失败。
    result = run("coverage", "json", "-o", str(OUTPUT / "combined.json"), env=env)
    report = run("coverage", "report", env=env)
    return result or report


if __name__ == "__main__":
    raise SystemExit(main())
