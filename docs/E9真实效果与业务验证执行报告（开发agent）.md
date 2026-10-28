# E9 真实效果与业务验证执行报告（开发 agent）

- 日期：2026-10-28
- 分支：`feat/e9-benchmark`（从 `3c4ffdc` / 计划基线 `bb38dee` 之后创建）
- 协议：[`E9评测协议`](E9评测协议.md)
- 计划：[`E9真实效果与业务验证执行计划（开发agent）`](E9真实效果与业务验证执行计划（开发agent）.md)
- 结论：**S9.0 已冻结；S9.1–S9.3 只完成可离线验证的工具与脚手架；没有执行正式 live 实验，
  没有质量或收益结论，M14 保持未完成。**

## 一、完成情况

| 阶段 | 状态 | 实际交付 | 未完成 |
|---|---|---|---|
| S9.0 协议 | ✅ | 协议 v1、指标 / 判定 / 预算 / 退出码、检索契约 §十二、ADR 0019、评测文档更新 | — |
| S9.1 数据 | 🔄 | `evals/e9-v1/` 草稿包、Pydantic 记录、hash / 分区 / 全库审阅 / 无答案 / 授权校验、模板、封存脚本 | 100 条授权素材、50 个自然任务、约 5,000 个任务—素材关系的人工确认与冻结 |
| S9.2 工具 | 🔄 | 四策略共享库 / 线索 / 查询向量；nDCG、Top-1、Hit@5、空结果、nearest-rank、分组 bootstrap；evidence / quality / benefit；持久费用账本、本地转发服务；统一 CLI；离线测试 | 隔离容器 live runner、真实价格快照、正式开发集 / 留出集；S8.3 正式切换未完成 |
| S9.3 工具 | 🔄 | 20 任务 / 32 次 E2E 证据结构、`e2e` 离线校验、单任务真实 UI 尝试器（新建会话 → 提交 → 刷新 → 证据）；假模型隔离 | 未在本机启动真实 API / Redis / worker 跑通；S7.29 未签收；正式实验未执行 |
| S9.4 执行包 | ⬜ 仅模板 | 同意书、主持脚本、任务表、采纳 / 失败记录、脱敏汇总模板 | 未招募、未取得同意、未开展真人试用；S11.1 未完成 |

## 二、可复现的验证

在本机工程根执行：

| 命令 | 结果 |
|---|---|
| `uv run pytest -q` | 1495 passed / 3 skipped / 400 deselected |
| `uv run ruff check .` | 通过（退出码 0；仅对用户遗留的受限目录打印权限警告） |
| `uv run mypy` | 通过（80 个 source files） |
| `uv run pytest --cov=xhs_agent --cov-fail-under=0` | 单测覆盖率 66%；**不是联合门槛**，集成未跑 |
| `uv run pytest -m integration` | **未执行**：Docker Desktop API 不可用（`CreateFile` 找不到文件），环境失败 |
| `uv run python scripts/eval_e9.py validate --experiment evals/e9-v1 --allow-draft` | 退出 0，提示 draft |
| `... validate`（严格） | 退出 3：数据未冻结，符合预期 |
| `... estimate --experiment evals/e9-v1` | 退出 0；按预期 token 估算约 **0.9844 元**（含 20% 重试余量）；价格来源为未核验的配置默认值 |
| `... retrieval --experiment evals/e9-v1 --live` | 退出 4：S8.3 未完成，拒绝 live |
| `... e2e --experiment evals/e9-v1` | 退出 4：没有 `results/e2e.json`，拒绝空证据通过 |
| `git diff --check` | 通过 |

**没有执行任何付费模型调用。** E9 账本支出 0 元、未知占用 0 元、剩余 10 元。

## 三、正式实验为什么没有执行

1. **S9.1 数据未成立**：当前 `evals/e9-v1/cases.json`、`materials.json`、`qrels.json` 为空；
   没有用户确认的相关性标注。用合成素材或根据算法结果补标注会直接违反协议。
2. **S8.3 未正式关闭**：E8 的正式数据库切换仍待单独授权；S9.2 正式实验依赖它，M13 不能提前关闭。
3. **S7.29 未签收**：S9.3 正式实验依赖真实多轮专项；不能用 E9 自测替代。
4. **预算与代理未取得真实价格 / 上游凭据**：配置默认单价不是账单价格；live 必须先保存
   官方价格快照，并通过转发服务的端点与跨进程测试。
5. **集成测试环境失败**：本机 Docker 不可用，无法跑真实 Postgres / Redis / worker 路径；
   按 AGENTS 记录为“未执行”，不静默跳过。

因此本轮没有“Top-1 / Hit@5 / nDCG / 成功率”真实数字，也不判断混合策略是否优于简单基线。
负面或正面结论都必须等正式实验。

## 四、交付物索引

| 位置 | 内容 |
|---|---|
| `docs/E9评测协议.md` | S9.0 协议 |
| `docs/contracts/检索契约.md` §十二 | 评测用通道选择与共享查询向量 |
| `docs/adr/0019-e9-evaluation-package.md` | 评测内部包决策 |
| `docs/评测集.md` §九 | E9 与旧 v1 的边界 |
| `evals/e9-v1/` | 数据草稿、模板、前置状态 |
| `src/xhs_agent/evaluation/` | 模型、校验、指标、判定、账本、转发、四策略、E2E、CLI |
| `scripts/eval_e9.py` | 统一 CLI |
| `scripts/e9_model_proxy.py` | 本地模型转发 + 费用守卫 |
| `scripts/e9_browser_attempt.py` | 单任务真实 UI 提交、刷新与证据记录 |
| `scripts/e9_seal_dataset.py` | 人工确认后的数据封存 |
| `docs/e9/s9.4/` | 真人试用执行包 |
| `tests/unit/test_e9_*.py` | 62 个 E9 离线用例（含预算 / 代理 / 数据 / 判定 / 四策略编排） |
| `runs/_e9/`（忽略） | 本报告验证日志、覆盖率、估算输出 |

## 五、风险与后续

- 本地转发服务只做了 MockTransport 测试，未对真实供应商端点验证 SSE / usage 差异；live 前必须补。
- `run_retrieval` 的数据库路径未在本机 Docker 下验证；需要恢复 Docker 后跑集成测试。
- live 阶段的 index / retrieval / e2e 命令目前只做门禁并返回 4，没有接隔离容器；
  这是未完成项，不应被读取为“已可执行正式实验”。
- 数据封存脚本要求逐字确认短语；封存后仍须人工复核 manifest hash。
- 后续顺序：补数据与人工 qrel → 通过严格 validate / estimate → 完成 S8.3 切换 → 冻结候选 →
  开发集 → 留出集 → S7.29 签收后 S9.3 → S9.4。M14 最早在这些证据齐备后关闭。
