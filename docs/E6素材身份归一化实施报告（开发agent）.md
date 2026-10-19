# E6 主题三 · 素材身份归一化实施报告（开发 agent）

**报告日期：**2026-10-19

**接手基线：**`04408b8`（E6 接力交接文档；工作区含相对 `5911c32` 的未提交实现初稿）

**开发状态：**离线实现、契约、迁移与临时库演练完成自验；**正式库未应用，真实媒体未改动。**

**整体判定：****M12 仍未完成，S8.0 不可开工，E7 M18 保持原待验收。** 本报告是开发自验，不是独立验收。

---

## 1. 完成内容

### 1.1 交接缺口（文档 §5.1）

| 缺口 | 处理 |
| --- | --- |
| Ruff 4 项导入问题 | 已修复；`ruff check .` 全绿 |
| 旧集成夹具使用绝对素材路径 | 已改为规范键：`test_materials_api`、`test_materials_sync`、`test_materials_index`、`test_retrieval`、`test_keyframes`、`test_run_writeback`、`test_workflow_smoke`、`test_chat_tools_realdb`、`test_context_followup`、`test_api_endpoints`、`test_embedding_backfill`；迁移测试保留旧格式样本 |
| `test_migrations` 期望 revision / 新结构覆盖 | 更新到 `0013`；新增扩展结构往返、约束门禁、批次存在时拒绝 downgrade 三组用例 |
| `test_probe` 未 stub `check_material_identity` | 已 stub；新增启动检查集成用例（正常放行 / 冲突阻断） |
| 旧 ID API / 上下文 / 改稿闭环 | 新增集成用例：旧 ID 的详情 / 编辑 / 关键帧 / 回收站 / 同路径新素材不接管；改稿按基础文案旧 ID 解析到规范素材且模型看不到旧 ID |
| 前端生成类型 / 质量门 | `gen:api` 已纳入生成物；`lint` / `test`（158 passed）/ `build` / `check:api` 通过（见 §3） |

### 1.2 首轮审查问题（文档 §5.2）

| 复核点 | 结论与改动 |
| --- | --- |
| 改稿模型输出素材 ID | 已确认原 `Draft.from_dict` 允许模型覆盖 `material_id`。`copy_revise` / `copy_draft` 解析现在拒绝与基础/选中素材不一致的 ID；补充单测 |
| 文件操作全部边界 | 旁车写入按素材根重新解析并拒绝链接；旁车迁移 / 回收站拒绝链接；上传月份目录落盘前按根解析（junction 用例通过）。关键帧：工程根相对、索引目录不在工程根下时保留绝对路径，不再写 `../` 逃逸 |
| 归档与摘要精度 | 新增 `decode_record` 严格逆变换（vector 文本 → 浮点数组、numeric 文本 → Decimal）；保留 `jsonb_populate_record` 回填测试；归档清单逐行摘要 |
| 归档校验证明原始完整性 | 计划新增 `archive_manifest`（实体 / 原主键 / 摘要）。apply 在破坏性改写前按清单核验；新增"改内容并重算自带摘要 / 删一条"均失败的测试 |
| 跨批次别名与审计 | 后续批次重指向旧别名时归档旧别名行；`verify` 允许"本批 target 现解析到 current"的单跳重指向，全局图仍拒绝链 / 环；精确模式只适用于最新批次，历史批次用 `--audit-only`；新增两批次再合并测试 |
| 代码与计划版本绑定 | CLI 记录 `HEAD+worktree:<摘要>`（源码 diff、未跟踪脚本内容与状态）；apply 用当前实现重建计划，实现变化即拒绝；新增版本格式 / 稳定性单测 |
| 预检报告完整性 | 计划增加旧 JSON 引用逐行盘点、关键帧素材、关联运行、旧引用 / 关键帧 / 运行计数；Markdown 列出缺失文件、数据库身份来源与问题清单 |
| 历史根映射能力 | 根映射自身校验（相对根、`..`、UNC 混用 POSIX、非法相对键）；重叠映射必须同键；补充参数化单测 |
| 数据库权限与错误报告 | `pg_control_system()` 权限不足时记录 `cluster_source=unavailable`，不伪造集群身份；CLI 错误输出不含 DSN；Windows GBK locale 下 git 输出强制 UTF-8 |
| 消费方覆盖与缓存稳定 | 修复 `dedupe_by_path` 的 CWD 绝对化 / 小写合并（改为规范键、大小写敏感）；删除 CWD 依赖的死代码；`load_material_frames` / 详情 / 编辑 / 回收站统一走别名解析 |

### 1.3 迁移与约束

- `0012_material_identity_archive`：批次、归档、别名三表（初稿保留，未改旧迁移）。
- `0013_material_identity_constraint`：规范路径 CHECK 增加**段首尾空白、DEL** 拦截；旧格式行存在时升级明确拒绝，必须先 plan / apply / verify。
- 空库 `upgrade head` / `downgrade` 往返通过；已存在身份批次时 `0013 → 0012` 拒绝；`alembic heads` 为单一 `0013`。
- 临时库演练：plan → apply → verify（含 CLI 子进程）通过；篡改计划被拒绝且返回码 1。

## 2. 关键设计补充

- **归档清单**：`archive_manifest` 与 `expected_records` 随计划摘要签名；归档先核验再合并。
- **多批次可验证范围**：精确核验只对最新批次有意义；旧批次审计模式仍核验归档、别名、规范路径。
- **实现绑定**：计划与 apply 均绑定当前工作区实现指纹；审查后重新生成最终计划。
- **别名语义**：单跳、无环；被后续批次再合并时重指向并归档旧行；当前素材删除不删别名。

## 3. 质量门与证据（本机，2026-10-19）

```powershell
# 工程根 D:/agentxiangmu/project
uv run ruff check .                     # All checks passed
uv run mypy                             # Success: no issues found in 67 source files
uv run pytest                           # 1401 passed, 2 skipped（跳过项为无权限创建文件符号链接的用例）
uv run pytest -m integration            # 见下（临时 Postgres / Redis）
uv run python scripts/check_openapi.py  # 契约一致性通过
uv run alembic heads                    # 0013 (head)
git diff --check                        # 通过

# frontend
pnpm run gen:api                        # 生成物已纳入本批
pnpm run lint                           # exit 0
pnpm run test                           # 15 files / 158 passed
pnpm run build                          # 通过（vue-tsc + vite）
pnpm run check:api                      # 提交后验证（生成物与契约一致）
```

**集成结果：**`316 passed`（本报告落盘前最后一次全量，7m10s）；新增用例覆盖迁移门禁、归档清单 / 精度、多批次别名、旧 ID 服务链、CLI plan/apply/verify、启动检查、上传 / 旁车边界。`test_http_pool` 在无 `NO_PROXY` 的宿主机代理环境下会得到 502；测试已为 loopback 显式设置 `NO_PROXY`，属环境隔离修复，不是产品降级。

**未执行 / 未完成：**

1. 正式库停写切换、`pg_dump` / `pg_restore` 恢复演练与旧版本读回（无正式库授权）。
2. 跨环境（宿主机 ↔ 容器）同一素材库交替索引、真实浏览器全栈闭环、性能样本。
3. 无权限账号下的 `pg_control_system()` 降级路径（代码已处理，缺低权限角色实测）。
4. 文件符号链接用例在无创建权限的 Windows 环境跳过；junction 上传用例已通过。
5. 上下文状态里 `object_refs.material_id` 的旧引用消费方（当前上下文对象键是 draft/clue，未发现读取素材 ID 的运行时路径）；如需支持需先补契约。
6. E8 交接包（批次导出摘要、浏览器截图、恢复记录、已知旧数据限制）与独立评审。
7. 覆盖率门槛未单独执行；未新增付费模型调用。

## 4. 风险与边界

- 计划与实现的指纹绑定意味着**任何实现改动都会使旧计划失效**；正式切换必须在最终已审查提交上重新生成计划。
- 精确 `verify` 只适用于最新批次；多批次环境下旧批次只能 `--audit-only`，这是刻意收窄的可验证范围。
- `0013` downgrade 在存在素材或批次时拒绝；schema 回退不等于数据恢复，恢复走切换前备份。
- 别名重指向保留原 `batch_id`，历史重指向记录在归档行里；审计需同时看归档。
- 正式库 apply 必须停写并完成备份演练；代码未改变删除级联与恢复新 UUID 的既有行为。

## 5. 交付标识

- 本地提交：见 `git log -1`（本报告与实现同一提交；未推送、未合并、未部署）。
- 正式库：**未应用**；真实媒体：**未改动**。
- E8 开工条件：**不具备**（缺正式切换、恢复演练、浏览器 / 性能证据与独立验收）。
