"""E9 盲标表导出 / 回填导入（S9.1）。

用法：
    uv run python scripts/e9_labels.py export --experiment evals/e9-v1 --out labels.csv
    uv run python scripts/e9_labels.py import --experiment evals/e9-v1 \
        --csv labels.csv --reviewer 王老登

导出是任务 × 素材完整矩阵；导入必须逐格齐全，grade=1/2 必须写理由。导入不冻结版本，
之后由 `e9_seal_dataset.py` 统一封存。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from xhs_agent.evaluation.dataset import load_dataset
from xhs_agent.evaluation.labels import (
    build_qrels,
    export_rows,
    qrels_payload,
    read_csv,
    write_csv,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E9 盲标表")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--experiment", required=True)
    export.add_argument("--out", required=True)
    export.add_argument("--force", action="store_true")
    load = sub.add_parser("import")
    load.add_argument("--experiment", required=True)
    load.add_argument("--csv", required=True)
    load.add_argument("--reviewer", default="")
    args = parser.parse_args(argv)
    root = Path(args.experiment)
    try:
        dataset = load_dataset(root)
        if dataset.manifest.status == "frozen":
            print("已冻结版本不能原地改标注；请新建版本", file=sys.stderr)
            return 3
        if args.command == "export":
            out = Path(args.out)
            if out.exists() and not args.force:
                print(f"{out} 已存在；确认要覆盖时加 --force", file=sys.stderr)
                return 3
            rows = export_rows(dataset)
            write_csv(out, rows)
            print(f"导出 {len(rows)} 格 → {out}")
            return 0
        rows = read_csv(Path(args.csv))
        qrels, reviews = build_qrels(dataset, rows, reviewer=args.reviewer)
        target = root / "qrels.json"
        target.write_text(json.dumps(qrels_payload(qrels, reviews), ensure_ascii=False,
                                     indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"导入 {len(qrels)} 条 qrel / {len(reviews)} 条审阅 → {target}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"E_E9: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
