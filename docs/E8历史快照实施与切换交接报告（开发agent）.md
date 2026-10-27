# E8 历史快照实施与切换交接报告（开发 agent）

> 本文保留 2026-10-20 的开发自验历史。2026-10-27 独立验收发现的问题已修复，最终单测 1429、真实集成 395、联合覆盖率 86.88%、前端 171、隔离镜像回填和浏览器通过；正式切换仍待明确授权。最新状态及具体范围见 [独立验收修复与正式切换审批包](E8独立验收修复与正式切换审批包（2026-10-27）.md)。下文旧测试数量和“独立验收未进行”不代表当前结论。

- 日期：2026-10-20
- 基线：`98e4403`（E8 执行方案提交；E6 已正式切换并独立验收）
- 范围：`docs/E8数据正确性与历史快照执行方案（开发agent）.md` 的 S8.0–S8.3
- 结论：**实现与可执行自验已完成；正式数据库切换未执行，M13 保持未关闭，S8.3 仍待独立验收。**

## 1. 交付状态

| 批次 | 状态 | 证据 |
| --- | --- | --- |
| S8.0 契约与 ADR | ✅ | 数据契约 §3.3–§3.5/§3.9/§3.12、OpenAPI、ADR 0018 |
| S8.1-A 结构与迁移工具 | ✅ | Alembic `0014`/`0015`；`scripts/e8_snapshot.py` plan/apply/verify；迁移测试 |
| S8.1-B 运行快照与统一读取 | ✅ | 提交冻结、执行配置、候选摘要、报告只读投影；E8-01/03/04/07/08/11 回归 |
| S8.2-A 删除解耦与历史预览 | ✅ | 移除 `run_matches.material_id` 级联；运行范围关键帧路由；当前状态独立展示 |
| S8.2-B 改稿证据与前端适配 | ✅ | 基础版本证据优先、缺失 `historical_evidence_incomplete` 零模型调用；结果页 / 聊天卡片 / 版本面板适配 |
| S8.3 回归与验收 | 🔄 自验完成，独立验收/正式切换未完成 | 单测 + 集成 + 浏览器 + 恢复演练 + 性能 + 切换包 |

## 2. 实现要点

- **提交冻结**：`run_hotspots.raw_text/clue` 在提交事务内复制（显式线索优先，否则复制共享缓存）；共享 `hotspots.clue` 仅作缓存，历史读取只看运行行。
- **执行快照**：`runs.execution_snapshot` 保存模型 / embedding / 检索参数 / topk / prompt / 工作区摘要；`finish` 补齐 prompt 版本，不保存密钥或完整配置对象。
- **候选依据**：检索先读素材并生成 `material_snapshot` 与索引摘要，再调用解释模型；`run_matches.material_id` 是历史 UUID，无素材外键，删除 / 恢复 / 同路径新素材不改旧候选。
- **统一读取**：`load_run` 只读冻结字段；当前素材存在性、文件可读性与预览可用性作为独立附加信息；报告使用保存的配置、统计与缺口建议，不重算。
- **证据来源**：字段组独立记录 `runtime / legacy_run_record / migration_capture / cutover_capture / missing`；`completeness` 独立，旧数据缺失不补假值。
- **改稿**：基础版本 `evidence_snapshot` 优先；否则按 run + position + material_id 唯一定位（允许 E6 别名）；缺证据在模型调用前拒绝。
- **迁移**：E8 批次表与 E6 身份批次分开；计划确定性、数据库身份校验、源摘要比对、幂等、整体事务回滚；旧数据按 E6 归档 → 当前素材 → 缺失分级标源。

## 3. 验证记录（本次实际执行）

| 项目 | 结果 |
| --- | --- |
| 后端单测 | **1416 passed，3 skipped** |
| 后端集成 | **351 passed**（真实 Postgres / Redis / API / worker / 浏览器脚本） |
| 后端 lint / mypy | `ruff check .`、`mypy` 通过 |
| 前端 | **166 passed**；`lint`、`type-check`、`build` 通过 |
| OpenAPI | `scripts/check_openapi.py` 通过；`schema.d.ts` 已重新生成 |
| 覆盖率 | 单测 66.61%（低于既有 78% 单测门槛，基线同样不达标）；单测 + 集成合并 **86.54%**，达到 78% |
| 浏览器全栈 | 真实 FastAPI + Postgres + Redis + 前端 dist + Edge；**2/2 检查、exit 0**：[result.json](../../runs/_e8/browser-20261020T121247Z/result.json)、[候选截图](../../runs/_e8/browser-20261020T121247Z/run-candidates.png)、[报告 Markdown](../../runs/_e8/browser-20261020T121247Z/report.md) |
| 迁移 CLI | 空库 plan → apply → verify 通过；篡改计划拒绝（`test_e8_snapshot_cli`） |
| Compose / 镜像 | `docker compose config --quiet` 通过；`docker build -t xhs-agent:ci .` 成功（`sha256:8676…`） |
| 迁移恢复演练 | 含快照库 `pg_dump` → 另一容器 `pg_restore` → 批次与素材快照核验通过 |
| 性能 | 10 runs / 20 hotspots / 100 matches / 30 materials：plan 0.196s、apply 0.692s、verify 0.309s；`load_run` 0.141s、3 条查询、详情 16 KB、快照 86.6 KB：[performance.json](../../runs/_e8/performance.json) |

浏览器验证：历史运行页展示执行快照与证据来源、已删除素材的冻结标题和“原素材已删除”状态、当前素材历史关键帧可加载；HTML/Markdown 报告含同一历史文字依据与运行保存的模型信息。

## 4. 验收矩阵映射（主要）

| 编号 | 覆盖 |
| --- | --- |
| E8-01/02 | `test_two_runs_keep_their_own_clues_and_drafts`、`test_submit_freezes_clue_per_run`、`test_new_run_copies_cached_clue_at_submit` |
| E8-03/04/05/06 | `test_material_edit_and_delete_do_not_change_history`、`test_material_edit_during_explain_keeps_retrieved_snapshot`；同路径新 UUID 断言 |
| E8-07/08 | `test_report_uses_saved_config_and_gap_advice`、报告与详情同一投影 |
| E8-09/10 | 改稿证据绑定、基础版本降级、缺证据零模型调用（`test_missing_evidence_is_refused_before_any_model_call`） |
| E8-11 | 终态不同结果拒绝；旧执行代次写入拒绝 |
| E8-12 | 失败热点保留已完成依据；详情区分失败 / 合法空 / 缺失 |
| E8-13/14/15 | 迁移来源标签、计划/源摘要阻断、幂等 `already_applied` |
| E8-16 | `test_run_keyframe_route_checks_identity_and_version`（删除 404、版本不符 409） |
| E8-17 | 详情 / 报告共享业务字段；浏览器报告与页面同源 |
| E8-18 | 复用 E6 的同素材集跨根索引测试 |
| E8-19 | `test_e8_restore`（快照库备份恢复与核验） |

## 5. 未完成 / 阻塞

1. **正式数据库切换未执行。** 方案本身不构成 E8 切换授权；需要明确目标库、停写窗口、备份与回滚范围、版本部署和恢复服务授权。当前没有正式目标的数据库身份、备份校验和、最终计划与批次。
2. **独立验收未进行。** 本报告为开发自验，不能替代独立审查；S8.3 / M13 不能关闭。
3. **正式旧数据盘点未做。** 迁移工具与回填规则已在临时库验证；真实库的缺失分布、来源统计、计划摘要需在授权后只读盘点。
4. **覆盖率历史口径更正（2026-10-27）**：既有 CI 执行单测与真实集成的联合 78% 门槛，分项不单独设门槛。历史单测 66.61% 不应单列为阻塞；最新联合质量门与分项结果见上述审批包，未下调门槛。
5. **前端版本面板证据提示为轻量展示**；真实聊天改稿全链路使用固定 provider 的数据库测试覆盖，未做付费模型调用。

## 6. 正式切换包（待授权执行）

执行前必须补齐：

- 目标：数据库名 / OID / `pg_control_system()` 集群标识、Alembic revision、E6 批次与别名、E8 批次、素材根和全部写入进程。
- 停写：暂停 API，排空 worker，确认无在途任务、上传、编辑、回收站、索引或独立 CLI 写入。
- 备份：`pg_dump -Fc`、校验和、工具版本、代码提交；恢复到隔离库验证 `verify_snapshot` 与历史详情 / 报告可读。
- 命令：`python scripts/e8_snapshot.py plan --output <plan.json>` → 审查计数 / 来源分布 / `executable` → `apply --plan` → `verify --batch <id>`；E6 已应用的旧库按 `0014`/`0015` 顺序升级。
- 回退阈值：apply 事务失败保持停写并整体回滚；apply 成功但核验失败优先向前修复；不得直接启动不兼容旧代码；schema downgrade 不等于数据恢复。`0015` 存在孤儿候选时拒绝回退。
- 切换后：只读检查旧运行详情 / 报告 / 别名 / 缺失标记 / 可用预览，恢复服务并记录时间、批次、备份校验和。

正式切换前先完成独立验收；未取得授权时保持现状。

## 7. E8 交接边界

- 规范 UUID 与 E6 别名保持；E8 不清理 E6 归档。
- `migration_capture` 只证明迁移时记录；`cutover_capture` 只证明切换采集；都不是运行时快照。
- 旧运行无法证明的模型 / 检索配置、共享线索、缺口建议保留 `missing`，不伪造。
- 历史媒体不复制；素材删除后不通过缓存提供预览，同路径新素材不自动绑定旧候选。
- 正式切换与独立验收完成前，M13 不关闭，E7 M18 状态不变。
