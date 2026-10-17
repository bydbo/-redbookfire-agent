# context-repair-v2 · E7 第六批修复实施报告

**日期：**2026-10-17
**实施者：**开发 agent（一名，顺序实施）
**结论（先读）：开发自验通过；整体验收**不通过**（待独立评审 + 真实评测）。

## 一、范围与提交

- 修复 E7CTX-01–17 的可开发部分；E7CTX-18 完成协议冻结、回归与证据采集，
  付费运行与浏览器验收**待授权 / 待执行**。
- 代码基线 `cbd13f9` → 本轮提交（见 git log）；协议冻结于
  `evals/context-repair-v1/`（manifest 含 SHA256，`paid_runs_authorized=false`）。
- 保留无关改动：工作区中两个 E3 文档删除未纳入提交。

## 二、逐项处理（18 项）

| 编号 | 处理 | 关键代码 | 回归 |
| --- | --- | --- | --- |
| 01 撤回不计变更 | ✅ 已修复 | `core/context_rules.py`：DeltaResult 增加 retracted/superseded，changed 纳入；重复撤回=noop | `test_context_repair_regressions.py::TestCTX01*`、HTTP `TestCTX01RetractHttp` |
| 02 状态截断/自动归档 | ✅ 已修复 | 移除 `MAX_FACTS_PER_SCOPE`/`_cap_facts`；`render_state`/`state_prompt` 全量 | `TestCTX02NoTruncation`（41 条全装入） |
| 03 遗留冲突/CAS/验证失败 | ✅ 已修复 | `_prepare_context` 状态化（confirmation_required/conflict/invalid）；apply_delta 两段式整批拒绝；API 提交 CAS 竞争 409 | `TestCTX03*`、HTTP `TestCTX03CasConflictHttp` |
| 04 确认冲突不处理旧项 | ✅ 已修复 | `apply_action` confirm 按 `replaces_fact_ids` 原子替换；旧项已变→conflict（409） | `TestCTX04*`、HTTP `TestCTX04ConfirmReplaceHttp` |
| 05 旧会话恢复 | ✅ 已修复（开发自验） | `load` 区分 legacy pending；`recover` 分页（≤20 条完整消息、游标、上界、账目 assistant 行）；`complete_recovery`（候选清零+缺口显式确认） | HTTP `TestCTX05LegacySessionPending`；分页恢复的服务级闭环已实现，**真实模型分页运行待授权** |
| 06 content 读取回落 | ✅ 已修复 | `artifact_versions.normalize_draft_content`（统一 Draft + 两种包裹兼容 + 双包裹拒绝，无回落）；revise 存完整 Draft | HTTP `TestCTX06And07VersionChain::test_three_revisions_*` |
| 07 旧版误判冲突/MissingGreenlet | ✅ 已修复 | 认领固化 expected_head/state/source_message；提交比较**当前 head vs 生成前 head**；refresh+锁内重读标量 | HTTP `test_head_changed_during_generation_*`、`test_replay_returns_original_base_reference` |
| 08 抽取消费漏计 | ✅ 已修复 | `run_turn` 不清空注入账本；抽取记录/版本进图 | 设计回归：账本贯穿四条收尾路径（既有账目投影测试保持绿） |
| 09 改稿/修复未过预算 | ✅ 已修复 | `StructuredCaller.before_request` 每次实际请求（含修复）执行；轮次入口 `before_tool_request` 统一接入 | `TestCTX09BeforeRequestBudget` |
| 10 附件校验绕过 | ✅ 已修复 | 上传保存 sha256/bytes/mtime_ns；`ensure_digests` 归属+存在+内容变化三道校验；L1 命中前同样校验 | `test_context_artifacts_cache.py::TestEnsureDigests`（缺文件/越界/改写各一例） |
| 11 缓存到期/并发分类 | ✅ 已修复 | `generation_id`；命中先验结构（invalid_payload=miss+失效事件）；冲突时到期/无效→条件重建，有效→沿用 | `test_context_storage.py::TestImageCache`、`TestEnsureDigests` |
| 12 线索编辑/选择/重解析 | ✅ 已修复（开发自验） | `POST .../artifacts/clue-versions`、`POST .../artifacts/{id}/select`；选定编辑版优先消费；`force_reparse` 绕过；首份原始解析登记 clue 版本 | 服务层实现 + OpenAPI 契约；**浏览器交互待执行** |
| 13 指标分母 | ✅ 已修复 | C 来自 `cache_request_started`；创建队列 `cache_entry_created`（按 generation）；miss 不进复用集；`--strict` CLI | `scripts/context_metrics.py` 重写；固定事件集复算见下文"指标" |
| 14 尝试观测/能力/价格 | ✅ 已修复 | `model_attempt_started/finished`（逐次传输、price 快照）、能力开关（prefix/stream/thinking，默认 false）、incomplete 计入 supported 分母 | `chat_turns._emit_model_attempts` |
| 15 模式不统一 | ✅ 已修复 | `core/context_policy.TurnContextPolicy` 一次解析贯通；shadow 不改历史装配/不登记版本/不回持久缓存；窗口未核实降级 shadow+reason | `TurnContextPolicy` 单元口径 + 模式分档测试 |
| 16 前端不足 | 🔶 部分 | 恢复面板（开始/继续/完成）、版本选择绑定 operation_binding、删除会话、409 刷新、action_id 幂等；**线索编辑器 UI 与来源定位跳转未做** | `stores/chat.ts` / `ChatView.vue`、前端 153 测试绿 |
| 17 对象/话题闭环 | ✅ 已修复 | `select_object` 动作 + `_validate_object_ref`（run 归属会话校验）；reset_topic 归档 topic 项保留 session 项；"换一个"仍转提案 | `TestCTX17ObjectRefValidation` |
| 18 验收证据 | 🔶 开发可执行部分完成 | 协议冻结 + 12 模板 + manifest；复现回归 19 条（12 单元 + 7 HTTP）；**真实多轮/前缀/浏览器/人工评分待授权执行** | `evals/context-repair-v1/` |

## 三、验证记录（2026-10-17，最终代码）

| 命令 | 工作目录 | 结果 |
| --- | --- | --- |
| `uv run ruff check .` | 工程根 | ✅ All checks passed |
| `uv run mypy` | 工程根 | ✅ 63 文件无错误 |
| `uv run pytest` | 工程根 | ✅ **1310 passed**（含新增 19 条修复回归） |
| `uv run pytest -m integration` | 工程根 | ✅ **250 passed**（真 Postgres/Redis；含 0009 迁移、HTTP 修复回归 7 条） |
| `uv run python scripts/check_openapi.py` | 工程根 | ✅ 通过 |
| `pnpm run lint` / `test` / `build` / `check:api` | frontend/ | ✅ 153 passed / 构建通过 / 类型一致 |
| 迁移 upgrade/downgrade | 临时库 | ✅ `test_migrations.py` 全绿（0009 往返） |

## 四、真实状态与指标

- 真库读回：撤回状态、审计、409 detail（reason/state_version/retryable）、恢复 pending 均经
  HTTP 读回验证（`test_context_repair_http.py`）。
- 指标：`scripts/context_metrics.py --strict` 的固定事件集复算由新增观测事件支撑
  （cache_request_started / cache_entry_created / invalidated / model_attempt_started）；
  **历史数据库未含新事件，strict 报告需在新产生的会话数据上运行**——本轮未生成真实
  指标报告（无付费运行），标待执行。
- 费用：本轮无真实模型调用，消费为 0；未知消费不适用。

## 五、限制与下一步

1. **待用户授权**：12 模板真实多轮（≥648 轮）、30 组前缀实验、恢复分页的真实模型运行。
2. **待独立评审**：R0 协议、R2 版本事务、R5 指标、最终证据——本报告不充当独立验收。
3. **待执行**：浏览器主路径（选旧版改稿 / 确认冲突 / 恢复 / 编辑线索 / 重解析）；
   前端线索编辑器与来源定位跳转。
4. 性能门槛（200 次装配 P95、100 次缓存查询 P95）未测量——待真实环境数据。
5. M18：保持**待验收**；"开发自验通过"不等于"独立验收通过"。
