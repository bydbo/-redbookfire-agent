"""把已完成人工确认的 E9 数据包封存为 frozen（唯一写 manifesto 的入口）。

用法：
    uv run python scripts/e9_seal_dataset.py --experiment evals/e9-v1 \
        --reviewer 王老登 --confirm "我确认 100 素材 / 50 任务 / 全库 qrel"

封存前会再跑一遍结构校验；封存后再跑严格校验，失败时恢复原 manifest，不留下半冻结状态。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

from xhs_agent.evaluation.dataset import (
    CORE_FILES,
    load_dataset,
    validate_dataset,
)

CONFIRM_PHRASE = "我确认 100 素材 / 50 任务 / 全库 qrel"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="封存 E9 数据版本")
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    if args.confirm != CONFIRM_PHRASE:
        print(f"确认短语不匹配；必须逐字输入：{CONFIRM_PHRASE}", file=sys.stderr)
        return 3
    root = Path(args.experiment)
    manifest_path = root / "manifest.json"
    original_bytes = manifest_path.read_bytes()
    original = original_bytes.decode("utf-8")
    try:
        dataset = load_dataset(root)
        report = validate_dataset(dataset, root, require_frozen=False)
        if not report.ok:
            for item in report.errors:
                print(f"ERROR: {item}", file=sys.stderr)
            return 3
        payload = json.loads(original)
        payload["status"] = "frozen"
        payload["frozen_at"] = dt.date.today().isoformat()
        payload["produced_by"] = f"{payload.get('produced_by', '')}；reviewer={args.reviewer}"
        payload["files"] = {name: _sha(root / name) for name in CORE_FILES}
        payload["counts"] = {
            "cases": len(dataset.cases),
            "materials": len(dataset.materials),
            "dev": sum(1 for case in dataset.cases if case.split == "dev"),
            "holdout": sum(1 for case in dataset.cases if case.split == "holdout"),
        }
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8", newline="\n")
        frozen = load_dataset(root)
        strict = validate_dataset(frozen, root, require_frozen=True)
        if not strict.ok:
            manifest_path.write_bytes(original_bytes)
            for item in strict.errors:
                print(f"ERROR: {item}", file=sys.stderr)
            return 3
    except (OSError, ValueError) as exc:
        manifest_path.write_bytes(original_bytes)
        print(f"E_E9: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    print(f"已封存：{manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
