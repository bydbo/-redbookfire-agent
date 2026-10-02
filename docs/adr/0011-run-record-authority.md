# ADR 0011 · 运行记录的权威源是数据库，RunStore 退化为产物目录

- 状态：已采纳
- 日期：2026-10-02
- 关联：`docs/contracts/数据契约.md` §3.3、`src/xhs_agent/tools/trace.py`、`src/xhs_agent/workflows/analysis.py`

## 背景

E2 之前，一次运行的记录全部落在 `runs/<run_id>/`：`state.json`（步骤、耗时、token、成本）+ `trace.jsonl`（逐条事件）。

E2 引入 `runs` / `run_hotspots` / `run_matches` 三张表之后，同一批信息出现了**两个来源**：S3.1 的 `run_analysis` 既写
`RunStore`（`state.json`），又把 `prompt_versions`、token、成本准备写进 `runs` 表。两个来源必然漂移——谁先写、失败时谁回滚、
E4 的 structlog 该以谁为准，都没定义；S4.1（按 run_id 贯穿日志）会直接撞上这个问题，越晚改越贵。

## 备选方案

| 方案 | 代价 |
| --- | --- |
| DB 与 RunStore 双写、都当权威 | 必然不一致；失败回滚语义无法定义 |
| 只留 RunStore，DB 只存指针 | API 的 `RunDetail` 要读文件，违背服务化目标；筛选与分页得自己实现 |
| **DB 为权威，RunStore 退化为产物目录** | 需要改 `trace.py` 的职责：`state.json` 停写 |

## 决策

- `runs` / `run_hotspots` / `run_matches` 是**运行状态的唯一权威源**：状态、耗时、token、成本、`prompt_versions` 一律以库为准；
- `runs/<run_id>/` 退化为**产物目录**：保留 `report.html` / `report.md`（供下载与分享）与 `trace.jsonl`（逐条事件，供排查），**停写 `state.json`**；
- API 与报告一律从库读状态，不再从 `state.json` 读。

## 后果

**正面**

- 单一权威源；E4 的日志贯穿有明确基准（`run_id` 直接对应库中的行）；
- 失败回滚语义清晰：库里的 `status = failed` 就是事实，不需要在文件里找"到底跑到哪一步"；
- 产物目录仍保留人工排查所需的原始事件流，不损失可观测性。

**负面**

- `tools/trace.py` 需要改造（去掉 state 落盘、保留事件流与产物登记），属 S3.4a 范围内的改动；
- 旧运行遗留的 `state.json` 不再被读取，只能当历史文件保留。

**后续**

- S3.4a 的验收里写明"按本 ADR 执行"；
- S4.1 接 structlog 时以库中的 `run_id` 作为关联键。