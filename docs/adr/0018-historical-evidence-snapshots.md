# ADR 0018：历史依据采用不可变证据快照，删除素材不级联历史候选

- 状态：已采纳（E8，S8.0）
- 日期：2026-10-20
- 关联：E8 执行方案、数据契约 §3.3–§3.5/§3.9/§3.12、OpenAPI `RunDetail`

## 背景

E6 只把“素材身份”归一化：`run_matches` 仍是指向当前 `materials` 的外键，运行详情读共享
`hotspots.clue`，报告按当前配置和向量覆盖率重算，改稿按基础稿 `material_id` 查询当前素材。
因此编辑、删除、恢复或换模型后，旧运行会跟着变；删除素材甚至级联删掉历史候选。

## 决定

1. 在 `runs` / `run_hotspots` / `run_matches` / `chat_artifact_versions` 上增加**不可变证据
   字段**，保存执行配置、冻结输入、候选素材摘要、建议与改稿依据；不在 GET / 报告渲染时回填
   或重算。
2. 每个字段组独立记录 `schema_version / origin / captured_at / source_ref / digest /
   completeness / missing_fields`。`origin` 固定为 `runtime`、`legacy_run_record`、
   `migration_capture`、`cutover_capture`、`missing`；完整度与来源独立，不把切换采集冒充
   运行时快照。
3. `run_matches.material_id` 保留为历史身份，但移除到 `materials.id` 的外键和删除级联；
   “当前素材是否存在、文件是否可读、预览能否确认”只作为读侧附加状态。不采用 `SET NULL`，
   也不在删除时抹掉原 UUID。
4. 历史读取统一走一个投影：运行详情、聊天结果卡片、HTML/Markdown 报告使用同一份冻结
   业务字段；当前状态与预览可用性单独表达，缺证明确实说缺失。
5. 运行范围关键帧路由按 `run / position / rank` 定位候选快照，再用当前 UUID 与指纹校验版本；
   不接受客户端文件路径，不对已删除素材回退到残留缓存。
6. 改稿优先使用基础版本自身的 `evidence_snapshot`；否则按运行 + 热点位置 + 素材 ID 唯一定位
   运行快照（允许已验证的 E6 别名）。线索、素材文字或基础稿不完整时，在模型调用前返回
   `historical_evidence_incomplete`，不退回当前素材或最新版本。
7. 旧数据回填是显式 `plan / apply / verify`：原有运行字段标记 `legacy_run_record`，E6 归档
   通过摘要校验后标记 `migration_capture`，当前仍存在的素材标记 `cutover_capture`；共享线索、
   缺口建议和检索统计不重算，缺失即缺失。E8 批次与 E6 身份批次分开存储。

结构分两阶段发布：`0014` 扩列/建批次表，`0015` 移除素材级联。回退会重新加回级联，因此存在
孤儿候选时拒绝 downgrade；schema 回退不等于数据恢复。

## 后果

- 历史运行在素材编辑、删除、恢复、重新索引、配置变化后保持文字依据与初稿不变；恢复素材
  产生新 UUID，不自动重绑旧候选。
- 旧运行可能显示 `missing` / `partial`；这是事实，不生成虚假标题、空线索或当前模型名。
- 报告不保证 HTML 字节或媒体链接永久不变；媒体不可用只影响预览，不影响文字依据。
- 迁移需要停写和单独授权；在正式切换、独立验收前不能关闭 M13。
