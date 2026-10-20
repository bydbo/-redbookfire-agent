# E6 验收修复与正式库切换交接报告（开发 agent）

- 日期：2026-10-20
- 基线：`6e8dee5`（`f12c110` 之后的计划提交）
- 范围：`docs/E6验收修复与正式库切换执行计划（开发agent）.md` 的 R01–R08、必需验收与切换包
- 结论：**修复与隔离环境验收已完成可执行部分；正式库切换未执行，M12 保持待验收，S8.0 不放行。**
  计划要求“满足全部门槛后才能关闭 M12、放行 S8.0”，当前仍缺正式切换、独立验收和 Linux 文件链接证据。

## 1. R01–R08 状态

| 编号 | 状态 | 实现 / 证据 |
| --- | --- | --- |
| R01 回收站链接逃逸 | ✅ 代码与 Windows junction 用例通过；⚠️ Linux 文件链接用例未执行 | `_trash_base` / `_trash_item` 从可信素材根逐层解析；列举、清空、恢复、移入统一拒绝链接；`tests/unit/test_material_path_safety.py::TestTrashBoundary`、`tests/integration/test_materials_api.py::TestTrash::test_linked_trash_root_rejected_without_changes` |
| R02 扫描失败中止 | ✅ | `MaterialScanError` + `os.walk(onerror=…)`；未递归 `listdir` 也明确失败；单测两个、集成 `test_scan_failure_keeps_rows_and_never_deletes` 断言行 / 指纹 / 索引时间不变 |
| R03 文件组移动与补偿 | ✅ | 媒体 + 旁车整组选名、整体预检、逆序补偿、`manual_review_required` + `runs/_material_identity/fs_failures.jsonl`；覆盖媒体冲突、旁车单独冲突、第二文件失败、补偿失败、数据库提交失败 |
| R04 无损数值 | ✅ | 规则 v3：JSON 文本快照 + `Decimal`、确定性 encoder、归档/候选直接绑定 JSONB；`test_archive_preserves_numeric_precision_and_isolated_refill` 逐字段比较 DB 真实值；旧 v2 批次保留旧口径核验并标注限制 |
| R05 别名与摘要完整性 | ✅ | 最新批次精确比较完整别名集合（缺 / 多 / 目标不符都失败）、计划摘要、归档清单与计数；额外别名与篡改计划用例；历史批次 `--audit-only` 有限语义 |
| R06 前端删除/恢复口径 | ✅ | 三个删除入口 + 批量统一提示“恢复后作为新素材入库，历史候选及旧引用不会自动恢复”；恢复成功 / 索引失败分开提示 |
| R07 `file_missing` 与批量统计 | ✅ | 单条缺失、批量移入 / 缺失 / 失败计数、失败项重试、刷新不清结果；前端单测与浏览器全栈均覆盖 |
| R08 数据库身份 | ✅ | 仅 SQLSTATE `42501` 视为不可用；低权限计划 `executable=false`；apply 在写入前要求计划与目标库均可读集群身份；跨集群拒绝；并发 apply 幂等 |

## 2. 验证记录（本次实际执行）

| 项目 | 结果 | 命令 / 位置 |
| --- | --- | --- |
| 后端单测 | **1411 passed，2 skipped** | `uv run pytest` |
| 后端单测覆盖率 | **68.80%**，未达 `fail_under=78`；基线 `6e8dee5` 为 69.49%，同样是未达门槛 | `uv run pytest --cov=xhs_agent --cov-report=term` |
| 单测 + 集成覆盖率 | **86.36%**，达到 78% | `uv run pytest -m integration -q --cov=xhs_agent --cov-append --cov-report=term` |
| 集成测试 | 见下方分项；`test_material_identity_performance` / `restore` 均通过 | `uv run pytest -m integration -q` |
| 后端质量门 | `ruff check .` / `mypy` 通过 | 工程根 |
| 前端 | 163 passed；`lint` / `build` 通过；`check:api` 在提交后应零差异（本次 OpenAPI 变更已重新生成 `schema.d.ts`） | `frontend/` |
| 浏览器全栈 | **7/7 检查通过，exit 0**；真实 FastAPI + Postgres + Redis + Celery worker + 固定 embedding stub，无付费模型调用 | [result.json](../../runs/_material_identity/browser-20261020T034220Z/result.json)、[截图目录](../../runs/_material_identity/browser-20261020T034220Z/) |
| 恢复演练 | 备份 0012 → 源库迁移 → 新容器恢复 → **旧提交 `6e8dee5` 代码读回**；3 素材 / 3 候选 / 1 热点一致 | `tests/integration/test_material_identity_restore.py` |
| 性能记录 | 300 素材 / 100 重复对 / 400 候选：plan 1.793s、apply 6.747s、verify 1.109s、scan 0.682s；Python heap 峰值 9.36 MB | [performance.json](../../runs/_material_identity/performance.json) |
| 跨环境索引 | 同一素材集的另一个绝对根（junction 视图）不产生重复行、不重新打标；**真实容器进程未执行** | `test_two_mount_views_share_identity_without_reindex` |
| R01 文件链接 | Windows 无 `SeCreateSymbolicLinkPrivilege`，2 个文件链接用例 skip；junction 用例通过；**未跑 Linux 隔离容器** | `tests/unit/test_material_path_safety.py` |
| 并发 | 并发 apply 只产生一个批次 | `test_concurrent_apply_creates_single_batch` |

浏览器覆盖：删除确认文案、单条 `file_missing` 提示、正常删除、回收站恢复（真实 Celery 索引）成功提示、详情删除入口、批量删除统计与身份语义。

## 3. 未完成 / 阻塞

1. **正式库切换未执行。** 没有明确的目标库身份、停写窗口和切换授权；按计划，文档派发不等于授权，真实数据库不可逆操作需要单独授权。切换包见第 4 节，尚未填入目标身份和最终备份校验和。
2. **独立验收未进行。** 本报告是开发自验，不能替代独立审查。
3. **Linux 文件链接隔离环境未执行。** 当前 Windows 会话不能创建文件符号链接，只验证了 junction 与目录级链接；计划验收矩阵要求“可创建文件链接的 Linux 隔离环境”。
4. **真实容器跨环境（宿主→容器→宿主）未执行。** 已用同一目录的两个绝对根视图验证身份稳定；应用镜像未构建，未跑容器内进程。
5. **覆盖率 CI 口径未通过。** 单测覆盖率 68.80% 低于 78%，基线也低于门槛（69.49%），不是本次新增代码单独造成；单测 + 集成全量口径为 86.36%。未下调门槛。
6. **旧规则 v2 批次无真实样本。** 代码保留 v2 核验与限制输出，但没有已执行 v2 批次可演练；规则 v3 计划必须重新生成。

因此：**M12 保持 ⬜ / 待验收，S6.10–S6.13 保持 🔄，S8.0 不得开工。**

## 4. 正式切换包（待授权执行）

执行前必须补齐并脱敏记录：

- 目标：数据库名 / OID / `pg_control_system()` 集群标识、schema revision、已有迁移批次、素材根与全部写入进程。
- 根映射：显式 `flavor + root` 列表；未知根或身份不可读时计划 `executable=false`，不得 apply。
- 停写：上传、编辑、回收站、分析前同步、索引 worker、后台任务与独立 CLI 全部停写并排空。
- 备份：`pg_dump -Fc` 校验和、工具版本、代码提交、schema revision；恢复到隔离库验证可读。
- 命令：`python scripts/normalize_material_identity.py plan --roots <roots.json> --output <plan.json>` → 审查 `.md` / `issues` / `executable` / 计数 → `apply --plan` → `verify --batch <id>`；已有批次只读返回。
- 回滚阈值：apply 事务失败即整体回滚并保持停写；apply 成功但核验失败优先向前修复，不能直接启动旧代码；必须恢复时先恢复到隔离库并验证，再取得单独授权。
- 验收：切换后启动检查、`verify`、素材 / 候选 / 历史表计数与摘要、恢复写入并记录时间。

本次未生成真实目标计划，因此没有可引用的正式批次号、备份校验和或切换时间。

## 5. E8 交接边界

- 规范身份：素材根相对 forward-slash 键；UUID 保留；旧 ID 通过别名解析。
- 完整别名映射摘要与 `migration_capture` 归档能力已具备；归档只证明迁移时记录，不是运行时快照。
- 已知限制：v2 批次无损性未验证；回收站恢复是新 UUID，历史候选不回来；删除级联语义未改。
- 独立验收前，不得把本报告当作 E6 完成或 E8 前置完成。
