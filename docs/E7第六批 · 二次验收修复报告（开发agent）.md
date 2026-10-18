# E7 第六批 · 二次验收修复报告（开发 agent 执行版）

**报告日期：**2026-10-18

**定位基线：**`11cdd0e`（首轮修复）；本轮工作区基于 `3872163`（仅新增问题清单文档）

**开发状态：**部分完成（A01、A02、A05、A06、A07、A08、A11 已实施并自验；A03、A04 已实施、证据不完整；A09 已实施并自验；A10、A12、A13 未实施）

**整体验收状态：****仍不通过 / 待重新验收；M18 保持待验收。** 本报告是开发自验，不是独立验收。

---

## 一、本轮实施范围与结论

| 编号 | 原编号 | 状态 | 交付内容 | 证据 |
| --- | --- | --- | --- | --- |
| A01 | E7CTX-01/05 | 已修复，自验通过（待独立验收） | 建会话写维护起点与 `recovery.status=complete`；首条用户消息补起点；`load` 按显式起点判定、坏字段降级 pending；`_commit` 默认继承 recovery 并同步旧 `recovery_status` | 单测 `TestA01RecoveryBaseline`；集成 `TestA01NewSessionReady`、`TestCTX05LegacySessionPending` |
| A02 | E7CTX-01/03/04/17 | 已修复，自验通过（待独立验收） | 迁移 `0010` + ORM 放行 `select_object` / `complete_recovery`；线索编辑 / 选定改为单事务提交（版本、head、选定引用、state_version、审计、动作终态）；竞争时整批回滚并标失败；恢复在飞/需重试返回**真实 202**；绑定严格解析 | 迁移集成 8 passed；`TestA02EventTypes`；context 集成 16 passed；chat API 集成 63 passed |
| A03 | E7CTX-05/08 | 已修复，固定 21 条回归**未交付** | 重写恢复分页：元组游标 `(created_at,id)`、上界锁定、pending/running/awaiting_confirmation 语义、页内账目先提交、全部 caller 记录合计、任一无效候选整页不推进、CAS 竞争如实失败、`complete_recovery` 检查扫描状态 | 现有 context 集成 23 passed；缺 21 条 / 空页 / 取消固定回归 |
| A04 | E7CTX-06/07/16/17 | 已实施，闭环证据不完整 | 来源用户消息的可信绑定随 `ToolContext` 进入工具；改稿校验 run 归属、对象键、基础版本；模型显式冲突 → `artifact_version_mismatch`；`read_run` / `revise_draft` 校验 run 属本会话 | 单测覆盖工具 schema 与既有改稿路径；缺 v1/D0→v2/D1 的 HTTP 端到端断言 |
| A05 | E7CTX-02/15/17 | 已修复，自验通过（待独立验收） | 唯一适用事实筛选（session / 当前 topic / 已验证产物）；supervisor、改稿、分析 worker 共用同一约束快照；`runs.context_snapshot` + 迁移 `0011`；`copy_draft` / `copy_revise` 升 v2 | `TestA05ApplicableFacts`；全量单测；既有 off / 冲突回归保留 |
| A06 | E7CTX-09 | 已修复，自验通过（待独立验收） | 抽取 / 恢复每次真实请求（含结构修复）挂 `before_request`；`utf8_bytes_v1` 估算器；预算与适配器共用同一 `max_tokens` | `TestA06`/estimator 断言；`TestCTX09BeforeRequestBudget` |
| A07 | E7CTX-10 | 已修复，自验通过（待独立验收） | 摘要 / 归属 / 存在性全过才置 `attachments_valid`；轮内键改为有序内容身份 + 模型 / 参数 / prompt / schema；失效清理并重走真实调用 | 单测断言 identity 键；既有图片缓存测试 216 passed 子集；缺各模式 × 持久开关的矩阵回归 |
| A08 | E7CTX-11/12 | 已修复，自验通过（待独立验收） | 选定版本优先于 L1/L2，无显式选择不再以 head 冒充；显式重解析调用一次并建立新原始业务版本（新 `version_no`、缓存新 generation），不覆盖用户选定编辑版；工具清单贯通 `force_reparse` / `clue_version_id` | 单测 + 既有 image cache / artifact 测试；缺 C0/C1/C2 全链端到端断言 |
| A09 | E7CTX-13 | 已修复，自验通过（待独立验收） | 度量脚本 v3：event_id 去重、请求级关联、单一主结果、命中消费率按请求、条目队列按创建代次、窗口半开、前缀尝试 started/finished 关联、strict 门槛 | `TestFixedEventRecompute` 8 passed（3/3、1/1、1/2、空报告失败、重复投递去重） |
| A10 | E7CTX-14 | **未实施** | 传输前 started / 终态回调尚未接入 provider / 视觉 / worker | 无 |
| A11 | E7CTX-08 | 已修复，自验通过（待独立验收） | 成功路径合并账本里已完成调用的真实 prompt 版本；抽取 / 恢复使用 prompt 资产真实版本 | `TestA11PromptVersionMerge`（101/21 tokens、0.011 元、两个版本） |
| A12 | E7CTX-16 | **未实施** | 前端历史预览 / 线索编辑 / 来源定位 / 恢复交互未做 | 仅重新生成 `schema.d.ts`，既有 153 条前端测试通过 |
| A13 | — | **未实施** | `evals/context-repair-v2/`、运行器、性能 / 浏览器脚本未交付 | 无 |

## 二、代码与契约

- **迁移**：新增 `0010_context_event_types`（扩展审计事件约束）与 `0011_run_context_snapshot`（运行级适用约束快照）；未修改 0008 / 0009。`alembic upgrade head` / `downgrade` 在临时 Postgres 通过（8 passed）。
- **契约**：`docs/contracts/数据契约.md`（事件类型、`context_state.recovery` 结构、`runs.context_snapshot`）；`docs/contracts/prompt契约.md`（`copy_draft` / `copy_revise` v2 与 `constraints_json`）；`docs/contracts/openapi.yaml`（`RecoveryProgress.baseline_*`）。
- **prompt**：`copy_draft`、`copy_revise` 升 v2，新增必需变量 `constraints_json`；`tools/prompt.py` 的 requires 校验仍生效。
- **旧数据兼容**：旧会话无 recovery → 首次恢复保持 `pending`（不批量标 complete）；旧 run 的 `context_snapshot='{}'` → 无约束、行为与之前一致；旧 `copy_*` prompt 版本记录仍可读。

## 三、验证命令与结果（本机，2026-10-18）

```powershell
# 工程根 D:/agentxiangmu/project
uv run ruff check .                  # All checks passed
uv run mypy                          # Success: no issues found in 63 source files
uv run pytest                        # 1326 passed, 251 deselected
uv run pytest -m integration         # 251 passed, 1326 deselected（244s）
uv run python scripts/check_openapi.py  # 契约一致性校验通过

# frontend
pnpm run gen:api                     # 重新生成 schema.d.ts（baseline 字段 + 202 响应）
pnpm run lint                        # 通过
pnpm run test                        # 153 passed
pnpm run build                       # 通过（含 vue-tsc 类型检查 + vite build）
```

- **新增回归**：`tests/unit/test_context_repair_regressions.py` 增加 A01 / A02 / A05 / A11 用例；`tests/unit/test_context_metrics.py` 覆盖 A09 固定事件；`tests/integration/test_context_repair_http.py` 增加 A01 新会话 ready 用例并修正旧会话模拟。
- **修正的既有断言**：`copy_draft` / `copy_revise` 升 v2 后，相关单测 / 集成断言与 `EXPECTED_VERSIONS` 同步更新；`runs` 列契约增加 `context_snapshot`。
- **集成分片结果**：`test_migrations` 8 passed；`test_context_repair_http` + `test_migrations` 16 passed；`test_context_repair_http` + `test_context_storage` 23 passed；`test_chat_tools_realdb` + `test_run_writeback` + `test_workflow_smoke` + `test_db_schema` 47 passed；`test_chat_api` + `test_api_endpoints` + `test_chat_storage` 63 passed。
- **全量集成**：`uv run pytest -m integration -q` → **251 passed，1326 deselected，244s**（临时 Postgres / Redis，含迁移 upgrade / downgrade）。
- **前端构建**：`pnpm run build`（含三段 vue-tsc 类型检查 + vite build）通过，构建产物写入 `frontend/dist/`（vite 提示部分 chunk > 500 kB，属既有告警）。

## 四、未完成 / 待验项

1. **A03 固定回归**：21 条分页、同 `created_at` 按 id、空候选页、扫描中取消、CAS 竞争、跨页撤回等固定样例尚未落测试。
2. **A04 闭环**：缺 HTTP 绑定 v1 + head v2 的真实消费者断言，以及跨对象 / 跨会话版本拒绝的端到端证据。
3. **A07 矩阵**：off / shadow / enforce × 持久开关、同路径改字节、删除、调序等矩阵未完整覆盖。
4. **A08 端到端**：C0 / C1 / C2 选定与重解析链路、TTL / prompt 失效未做真实库断言。
5. **A10**：未实施（需 provider 级传输观察回调 + 视觉 / worker 接线）。
6. **A12**：前端交互未实施；仅保证既有测试与生成类型一致。
7. **A13**：冻结用例、运行器、manifest、性能 / 浏览器脚本未交付。
8. **真实模型评测 / 人工语义评分 / 上游第五批独立签收**：未执行，按原方案列待执行；本轮未做任何付费调用。
9. **独立验收**：本报告只代表开发自验；M18 不签收，整体验收仍不通过。

## 五、风险与影响范围

- `_commit` 默认继承 recovery 后，任何调用方**显式传入** recovery 才能改变进度——新增调用点必须传，否则恢复状态保持不变（已在本轮恢复 / 完成动作中处理）。
- 迁移 `0010` 的 downgrade 在已存在 `select_object` / `complete_recovery` 审计行时会因约束不满足而失败；这是刻意边界，不能把 schema 回退当作数据完整恢复。
- 估算器改为 UTF-8 字节后，预算判定更保守：长中文上下文可能更早触发 `required_context_overflow`，属于预期行为；费用 `usage` 口径未变。
- `copy_draft` / `copy_revise` 升 v2 后，任何未同步传 `constraints_json` 的外部调用会按 prompt 契约缺变量报错。
- 恢复分页重写改变了 `pending` / `awaiting_confirmation` 的推进时机；若前端依赖旧的“每页完成即 awaiting_confirmation”，需要 A12 同步调整。
