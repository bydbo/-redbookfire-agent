# E7 第六批续工交付（2026-10-19）

**开发状态：A01–A12 工程修复及自动回归完成，A13 的运行器、冻结资产、固定模型对照、浏览器和性能证据已交付。整体验收仍待重新验收，M18 未签收。** 完整真实模型对照、独立人工评分和上游独立验收尚未齐备。可以按既有依赖接续 S6.10–S6.13；它们不依赖 M18，但自身尚未实施，不能据此宣布 E8 前置完成。

代码定位：工程 `D:/agentxiangmu/project`，续工起点 `64b04cf99d60f1f9f303486cb2827166769fc6e0`，包含未提交的昨日修复。工程修复提交 **`5e13f9c`**，后续单段提示词提交 **`0144e8e` / `d0f9911`**，各次提交钩子的 Ruff / mypy / 单测均通过，未推送。实际测试记录保留 `actual_ref` 和 `worktree_dirty=true`，候选源码内容指纹为 `42903f639061a81125d6bf87c64583c98229cce1a2edf9950e7d64aced9c9011`（当时 `src/` 中 `.py/.md/.json` 按路径及内容复算）；最终版本号整理见第 4 节，不回写早期测试代码标识。

## 1. 本次修复与逐项交付

原始复现、完整固定输入和验收规则沿用 A01–A13 修复方案（内部归档，不在本仓库），不改旧冻结资产或门槛。下表“自验”不代表独立评审签字。

| 问题 | 修复后的行为与主要入口 | 最终验证及剩余事项 |
| --- | --- | --- |
| A01 新会话 / 恢复信息 | `context_state.load` 区分新会话维护起点与无证据旧历史；状态更新保留恢复字段 | `test_context_storage.py`、新会话 / partial / 完成恢复回归；自验通过，独立待验 |
| A02 HTTP / 事务 | 编辑 201、重放 200；动作枚举与事件约束一致；版本、head、选定引用、状态、审计及终态同事务提交；异常后回滚并保留失败动作 | `test_clue_http_success_and_idempotent_replay`、编辑中途 / 终态注入失败、竞争测试通过 |
| A03 历史恢复 | 20+1 分页，同时间戳按 ID 续扫；空候选推进；后页撤回 / 替换前页候选；过期动作零调用；取消后显式 retry，两个 DB 会话仅一页实际发送 | `test_21_same_timestamp_empty_pages_and_cost`、真实 cancel / 竞争 / 跨页替换通过；真实模型完整分页语义待评 |
| A04 可信绑定 / 对象 | 从本会话来源用户消息读取绑定，读取失败终止；旧 v1 优先于 head v2；draft 位置真实存在或有本会话成功历史版本；version、material、clue 逐项校验；未解析对象确认前同样验证 | 实际改稿模型输入为 D0，重放返回原文且新增调用 0；跨会话和不存在位置拒绝；浏览器实际消息携带 v1 绑定 |
| A05 约束消费 / 回退 | session、当前 topic、本次 artifact 统一筛选进入改稿 / 投递 / worker 撰稿；已有待确认项不再阻止记录后续新限制；其他 artifact 的待确认项不阻断当前目标；reset 清旧对象、保留 session | 捕获实际 worker 模型请求，其他对象限制未混入；off/shadow 保留既有冲突与 partial 保护；语义保持率仍待真实评分 |
| A06 完整预算 | 抽取 / 恢复的每次请求及坏 JSON 修复都检查；记录观测后结束短事务；采用 `utf8_bytes_v1`；超限明确提示，零发送或保留首请求消费后停止修复 | 真实生产准备 / 恢复入口矩阵：充足 2 检查 / 2 请求，第二次超限 2 / 1，初始超限 0 请求；全套通过 |
| A07 轮内复用 | 每次读取先验附件归属、存在与内容；同内容不同路径可命中 L1；off/shadow/enforce 与持久开关分开处理 | 模式矩阵、同字节不同路径、删除 / 改写 / 顺序失效回归通过，命中新增视觉调用 0 |
| A08 线索身份与版本 | 精确读取选定编辑版 C1；新解析 C2 不覆盖编辑选择；显式选 C2 后才切换；模型 / prompt / TTL / 内容 / 顺序变化重新判缓存 | 真库矩阵、C0/C1/C2、HTTP 及浏览器闭环通过；无强制把非法 `text` 类型转合法值 |
| A09 指标 | 请求命中消费率、generation 条目复用率分别计算；选定编辑版不伪装缓存命中；过滤、零分母、缺终态和非法分项 strict 失败 | 固定事件复算与真实缓存生产者写库后复算通过；完整率不把未知写零 |
| A10 实际传输 | `core/model_attempts` 在每次 HTTP 前记录 started，成功 / 失败 / 取消记录 finished；`model_observations` 独立事务落库；同步视觉线程桥接；文本 / 视觉各自端点能力与价格 | 另一 DB 会话在传输内部已读到 started；worker、同步视觉、重试、取消测试通过；非云端独立验收 |
| A11 账本 | 成功抽取记录真实 prompt 版本；结构失败 / 取消保留先前工具调用消费；汇总已存在时不重复相加；未知单列 | `test_chat_turns.py`、`test_ledger.py`、生命周期及异常回归通过 |
| A12 界面 | 文案历史正文 / 旧版基础选择；线索结构编辑、选择和重解析；来源查看；恢复游标 / ID / 显式缺口确认；切会话清状态、迟到响应隔离、409 刷新、面板滚动 | 前端 158 项；固定 API 浏览器 6 组、真实 API 浏览器 5 组；实际查看 390px 截图，无横向溢出 |
| A13 最终证据 | 新冻结 v2、独立累计预算、组件 / 会话两种运行器、两臂固定模型、性能 / 浏览器脚本、人工待评表 | 工程证据完成；完整真实两臂、人工评分、上游独立评审仍待，不签 M18 |

原编号映射保持：01→A01/A02；02→A05/A06；03→A02/A04/A05；04→A02/A04；05→A01/A02/A03；06→A04；07→A02/A04；08→A03/A10/A11；09→A06；10→A07/A08；11→A08/A09；12→A02/A04/A08/A12；13→A09；14→A09/A10；15→A05/A07/A10；16→A04/A12；17→A02/A04/A05；18→A13。

## 2. 最终质量门

全部自动测试使用固定模型 / 传输，不访问真实模型。DB / Redis 使用临时 testcontainers，不迁移或清理用户业务库。以下是同一工作区的最终结果，不能和旧轮次数量相加。

| 检查 | 结果 | 证据 / 限制 |
| --- | --- | --- |
| 后端全量单测 | **1358 passed、288 deselected**，56.63 秒 | `runs/_e7b6_20261019/unit_verified.log`；提交钩子另按最终暂存内容检查 |
| 后端全量集成 | **288 passed、1358 deselected**，277.13 秒 | `integration_verified.log`，3 条既有 Celery `setex` 弃用告警 |
| Ruff / mypy | 通过，mypy 65 文件 | Ruff 对既有受限目录有访问告警，不清理陌生目录消除告警 |
| OpenAPI | `scripts/check_openapi.py`、`pnpm run check:api` 均通过 | 生成类型同步，check:api 在最终暂存后重生成无差异 |
| 前端 | **15 文件 / 158 测试通过**；ESLint、三套 vue-tsc、Vite build 通过 | vendor / echarts 仍有既有 >500 kB 构建告警 |
| 浏览器真实 API | **5 组通过**，无页面脚本错误 | `browser_verified/browser.json`、`database.json`、PNG；真实 API/PG/Redis，模型固定，不运行分析 worker |
| worker / 同步视觉 | 集成测试通过 | 真实业务入口 + 临时库 + 固定外部传输；不是付费供应商全链路证据 |
| 性能 | 状态装配 200 次 P95 **2.341 ms**；缓存读取 100 次 P95 **8.072 ms** | `performance_verified.json`，41 条事实、各预热 10 次；阈值 100 ms，仅本机临时库微基准 |

提示词分段升版后，`d0f9911` 提交钩子的完整单测通过，真实容器定向集成 **38 passed**（41.52 秒，`integration_prompt_final.log`）；两份模型可见正文一致性已断言。没有把旧版本号的测试记录改成最终版本。

第一次最终集成曾有两项连接池测试收到 **502**；测试本地 ASGI 端点只产生 200。仅在测试子进程为 `127.0.0.1,localhost,::1` 增补 `NO_PROXY` 后两项 **2 passed**，随后完整 **288 passed**。未修改系统代理或产品 HTTP 配置。失败日志 `integration_close.log` 保留，成功日志不能抹去该环境条件。

## 3. 冻结会话与运行器

`evals/context-repair-v2/` 保持 2026-10-18 冻结内容：12 条具体用例，T01/T02 各 24 轮，其余各 6 轮，每臂重复 3 次，两臂 **72 会话 / 648 轮**；manifest 校验未放宽。旧 `context-repair-v1` 与检索 manifest 均保留。

`scripts/eval_context_sessions.py` 经真实会话 API、状态、工具、PG 和 Redis 运行；固定模型与 `--live` 分开。T06 复用原 turn/call/source 验证同一动作重放；T08 建立本会话 A/B 和外会话真实 run；T09 实际种入 41 条事实并应用 1024 预算 / 16000 输出预留；T10 调真实准备入口并注入坏 JSON，第二请求重新检查。预期拒绝仍保留原失败行及分母，只在错误类别、归属、零新增发送等证据完全匹配时标记 `expected_stop_verified`。无关 404 或已发生发送不能冒充通过。

| 证据类型 | 代码与结果 | 正确解释 |
| --- | --- | --- |
| **固定模型旧基线** | 真正 `cbd13f9 + off`；36 会话 / 324 轮，退出 1 | `baseline_fixed_complete/sessions.json`；21 个执行器失败 / 不支持 / 缺少预期停止记录：T06 3、T07 3、T08 3、T09 9、T10 3。全部保留；不能解释成 21 个模型语义错误 |
| **固定模型候选** | 候选工作区 `enforce`；36 会话 / 324 轮，退出 0，执行器异常 0 | `candidate_fixed/sessions.json`；工程操作及停止规则完成；固定回复不评价自然语言约束遵守率 |
| **真实模型候选（部分）** | 同工作区候选 `enforce`，2 个完整会话及 1 个部分会话，**51 轮**，预算停止 | `live_sessions/sessions.json`；不是 72/648 完整效果验收，也没有旧代码真实臂；后续修复后未再付费重跑 |

运行器早期缺少旧基线 `register_version` 与 `ContextPrep.status` 兼容，曾提前中止；失败目录仍在。最终旧基线使用其原有 claim/commit 建相同 v2 夹具，缺少的准入能力如实记失败，未将候选实现注入旧代码。基线独立 checkout 曾位于本轮忽略目录，已验证干净后用 `git worktree remove` 回收；主工作区未被 checkout/reset，全部原始结果继续保留。

真实 T01/T02 暴露了未决提案阻断后续明确限制的问题，已将 enforce 流程改为“先保存本轮合法增量，再判适用冲突”，并覆盖真实数据库回归。当前真实记录也包含模型将普通描述转提案、任务未完成等现象；**没有人工评分，也没有证明最终候选达到 ≥95% 约束遵守率 / ≥90% 任务成功率**。不因结构化字段合法而给语义分。

## 4. 视觉与提示词

已保留早期失败：单图输出非法 `elements[].type="text"`，严格契约拒绝。运行器现在记录安全错误原因和已知 usage / prompt 版本；两条更早失败仅有 `UpstreamError`，没有补造原因。

实际视觉调用共 **9 次**：业务结果表 5 成功 / 3 失败，另有首次失败执行记录 1 条。最新单图开发 v3、多图开发 v2 各成功一次；这是小样本结构及视觉路径证据，不能宣称总体结构率 100%。固定测试继续保留非法类型拒绝及已发生消费。

提交按 prompt 契约拆分单段递增：单图 Constraints v2、Examples v3、Output Schema v4；多图 Constraints v2、Output Schema v3。最终单图 **v4** / 多图 **v3** 的模型可见正文，与最近实测的开发 v3 / v2 **逐字相同**，只增加头部版本号来完成分段版本记录；实测账目保留原版本，绝不改成新版本。原资产、规范化 SHA256 和一致性证明在 `runs/_e7b6_20261019/prompt_snapshots/`。

这些实测不是分析链路 20 条完整七维度回归，也不是最终新版本号的新增付费调用。prompt 完整效果门槛继续待验；同正文身份只证明内容相同，不替代人工语义和完整回归。`context_recover` v2 的累计待确认候选语义另有确定性跨页回归。未修改用户默认上下文模式。

## 5. 前缀、费用与未知

真实前缀实验 **30 组 × 3 = 90 请求**，90 个成功响应及 90 次实际尝试均有完整 hit/miss。输入 152640 tokens，其中命中 **136704**、未命中 **15936**，token 命中率 **89.56%**，该样本分项覆盖 90/90。第 0/1/2 次请求的中位总耗时为 **613 / 625.5 / 637.5 ms**；**没有观察到重复请求逐次提速**。请求是非流式，TTFT 为 null。组间共享大部分前缀，第 0 次不等同于完全冷缓存；不能据此估自然命中率或因果提速。

样本现有单一价格估算合计 0.333 元，不采用缓存折扣，未计算真实节省额；不是供应商发票。复算原始输入在 `_e7b6_final/model-results.json`，摘要在本轮 `component-summary.json`。请求/条目缓存指标另由生产事件按 `context_metrics.py` 复算，不能混用分母。

同一累计账本 `_e7b6_final/spend-ledger.json` 共 **188 条**：118 条已知 usage 的保守上界、70 条未知保留预留，总占用 **9.928456 / 10 元**，剩余约 **0.071544 元**。旧传输器未解析部分压缩响应，导致保守预留未释放；已补 SSE/gzip/deflate 解析及测试，旧未知未擅自结算或归零。账本锁已释放。**本轮不再调用付费模型，不能换目录重新获得 10 元额度。**

## 6. 契约、迁移与兼容

本次没有新增迁移编号，现有 Alembic head 为 **0011**：0008 / 0009 保存状态、版本、认领与缓存；0010 放行上下文动作审计事件，0011 保存运行级约束快照，后两项此前已随 `2c6f558` 存在。API 补齐动作响应枚举、编辑创建与幂等重放状态，前端类型从 OpenAPI 生成。数据契约说明可信对象、作用域、元数据 / 实际编辑字段、原子动作、缓存事件与端点观测；prompt 变更分段升版。

off 不新增正式状态 / 版本 / 持久复用，shadow 候选不改变正式结果；回退保留既有冲突和 partial 保护。旧版本内容兼容已知 Draft 包裹，无法识别即拒绝，不偷偷回落初稿。删除会话仍按既有契约处理附件与上下文数据，本轮没有实际删除用户会话、素材、未知目录或真实 DB 数据。

## 7. 继续进入 E8 的顺序

本轮未实施 S6.10–S6.13，当前素材绝对路径与重复身份问题仍存在。它们是 backlog 已有的独立主链，不需要等待 M18 签字：

1. **S6.10**：按当前 Alembic head 分配新迁移，冻结锚点 / 合并规则及逐行备份；先迁移 `run_matches.material_id`，再删重复素材行，保护历史候选。只在临时库验证，先形成可审查迁移。
2. **S6.11**：统一所有素材消费方的相对工程根路径及契约，覆盖索引、详情、旁车、回收站、检索和报告。
3. **S6.12–S6.13**：启动告警、单索引视角说明、临时库 upgrade / 幂等 / downgrade 边界、三项真机验收；真实数据去重另按备份与既有授权边界执行。
4. **S6.13 通过后再做 S8.0**：冻结历史线索 / 素材候选快照及删除语义，然后 S8.1→S8.2→S8.3。schema downgrade 不能承诺恢复已合并行。

E7 仍需并行保留的验收事项：完整真实两臂 72/648、独立匿名人工评分与不确定样本复核、上游第五批第四次修复独立验收证据、prompt 完整效果回归。本轮检索 `docs/` 与 `evals/reports/`，只定位到《第五批第三次修复未通过报告》与《第四份待修方案》（两者均为内部归档，不在本仓库），未找到第四次修复独立通过记录；不以本次自验代签。预算或人工未齐时保持未执行 / 待验，不新增阻断 S6 的虚构依赖。

## 8. 可重跑的命令与证据

在工程根执行，前端切到 `frontend`。本机 `.venv` 与现有 Node/Playwright 已可用，无需重装依赖。

```powershell
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m mypy
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
# 仅测试进程绕过本地代理；保留既有 NO_PROXY 项。
$env:NO_PROXY = (($env:NO_PROXY, '127.0.0.1', 'localhost', '::1') | Where-Object { $_ }) -join ','
.venv/Scripts/python.exe -m pytest -m integration -q -p no:cacheprovider
.venv/Scripts/python.exe scripts/check_openapi.py
.venv/Scripts/python.exe scripts/eval_context_repair.py
.venv/Scripts/python.exe scripts/eval_context_sessions.py --arm enforce --repeats 3 --out-dir runs/_e7b6_repeat/candidate
.venv/Scripts/python.exe scripts/benchmark_context.py --out runs/_e7b6_repeat/performance.json
# 浏览器模块 / 可执行文件通过 PLAYWRIGHT_MODULE、BROWSER_EXECUTABLE 指向本机既有安装。
.venv/Scripts/python.exe scripts/smoke_context_stack.py --out-dir runs/_e7b6_repeat/browser
# frontend/: pnpm run lint; pnpm run test; pnpm run build; pnpm run check:api
```

上述会话命令默认固定模型；不要自行添加 `--live`。真实续跑必须复用原累计账本，并先解决剩余授权额度不足的问题。旧基线用单独 `cbd13f9` checkout 的 `--source-root` 加 `--arm off`，不能把候选 off 叫旧基线。

全部原始结果在本地被忽略目录 `runs/_e7b6_final/` 与 `runs/_e7b6_20261019/`，其他机器拉 Git 不会自动得到。可提交的用例、运行器、脱敏报告和 hash 已随代码保存。真实输出未入库，人工表仍未评分。工作区原有两个 E3 文档删除、`.tmp_pytest_acceptance/`、`.workbuddy/`、PDF、`node_modules/` 保留，不纳入本任务提交。
