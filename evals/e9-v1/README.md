# E9 数据版本（e9-v1，draft）

> 状态：**脚手架，不是已完成的数据集**。当前没有 100 条授权素材、50 个自然任务或人工 qrel。
> 协议见 [`docs/E9评测协议.md`](../../docs/E9评测协议.md)；计划见
> [`docs/E9真实效果与业务验证执行计划（开发agent）.md`](../../docs/E9真实效果与业务验证执行计划（开发agent）.md)。

## 目录

| 文件 | 内容 |
|---|---|
| `manifest.json` | 版本、状态、计数与核心文件 hash |
| `materials.json` | 100 条素材的 key、相对路径、sha256、来源、许可、模型传输授权 |
| `cases.json` | 50 个自然任务、分组、分区、oracle、场景标签、E2E 标记 |
| `qrels.json` | 任务—素材等级、理由、审阅人、确认状态；以及每任务全库审阅记录 |
| `e2e.json` | 20 个 E2E 任务、6 个额外重复、追问 / 改稿标记 |
| `authorizations.json` | 已确认授权的素材 key 列表 |
| `prerequisites.json` | S8.3 / S7.29 / S11.1 的独立签收状态；不得代填 |
| `templates/` | 盲标表、案例和授权记录模板 |

## 冻结步骤（必须人工确认）

1. 把授权素材放进本目录的 `assets/` 或忽略目录；`materials.json` 的 `relative_path`
   必须是相对路径，`sha256` 必须匹配字节。私人原文件不能提交。
2. 先记录自然任务，再生成答案；禁止根据检索结果补写素材说明或拼查询。
3. 对每个任务检查**整个素材库**，填写 `qrels.json` 的 `reviews`（`reviewed_materials`
   必须等于库大小），并让用户确认最终相关性。允许稀疏保存正例，但审阅范围不能缩。
4. 严格无答案任务必须全库无 grade≥1；不能把 `must_hit` 为空当无答案。
5. 按 §2 的矩阵补齐 50 个任务（开发 20 / 留出 30）、四类输入和无答案数量；同组不跨集。
6. 选定 20 个留出任务进入 `e2e.json`，其中 6 个额外重复；至少 4 个无答案、5 个追问、
   5 个改稿，重复覆盖四类输入。
7. 运行 `uv run python scripts/eval_e9.py validate --experiment evals/e9-v1 --allow-draft`，
   修完所有错误；由用户确认后把 `manifest.status` 改为 `frozen`、填 `frozen_at` 和核心文件 hash。
8. 再运行 `validate`（不带 `--allow-draft`）和 `estimate`；通过后才允许任何 live 阶段。

`prerequisites.json` 只由对应验收人填写；E9 不自动把 S8.3 / S7.29 / S11.1 标完成。

## 数据接口

字段定义在 `src/xhs_agent/evaluation/models.py`，校验在 `dataset.py`；结构与协议冲突时以
协议和用户确认的标注为准，不能通过改校验器放宽分母或分区。
