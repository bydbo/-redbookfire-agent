# Prompt 契约（prompt 资产的唯一事实源）

> 本契约定义 prompt 文件的**结构、渲染规则、版本与变更门槛**，与另外四份契约并列：
> API 见 `openapi.yaml`，数据见 `数据契约.md`，检索见 `检索契约.md`，配置见 `配置契约.md`。
> 与根目录 `AGENTS.md` 冲突时以 `AGENTS.md` 为准。

## 一、适用范围与地位

**prompt 文本的唯一来源是 `src/xhs_agent/prompts/<task_id>.md`。禁止在代码里内联 prompt 字符串**，也禁止另起一套模板目录。当前适用十个任务：

| task_id | 用途 | 调用方 |
| --- | --- | --- |
| `material_tagging` | 素材关键帧 → 标题 / 标签 / 描述 | `tools/vision.py`（已落地） |
| `hotspot_clue` | 热点原文 → 爆点线索 | `agents/clue.py`（S3.1 落地） |
| `material_select` | 线索 + 候选素材 → 命中解释与建议用法 | `agents/explain.py`（S3.1 落地） |
| `copy_draft` | 线索 + 选中素材 → 文案初稿 | `agents/draft.py`（S3.1 落地） |
| `image_hotspot_clue` | 一张图 → 热点描述 + 爆点线索（多模态）；v5 收紧文字的九类枚举归类并补海报示例，输出 schema 明列枚举 | `services/image_clue.py`（S6.6 落地，S7.3 补登记） |
| `image_hotspot_clue_multi` | **一组图**（1–9 张）→ 一份热点描述 + 一份爆点线索；v4 收紧文字归类，输出 schema 明列九类枚举 | `services/image_clue.py`（`parse_image_clue_multi`） |
| `chat_supervisor` | 对话层：读用户消息 → 决定直接回答或调工具 → 把结果讲成人话 | `agents/supervisor.py`（S7.3 落地） |
| `copy_revise` | 已有初稿 + 用户要求 → 改后初稿（要求越界时只回拒绝理由，不给变通写法） | `agents/revise.py`（S7.8 落地） |
| `context_extract` | 本轮用户原文 + 现有状态 → 有限类型的状态增量（新增 / 撤回 / 显式替换 / 待确认提案） | `services/context_state.py`（S7.24 落地） |
| `context_recover` | 恢复页完整用户消息 + 已恢复候选 → 恢复候选增量（全部待确认，E7CTX-05） | `services/context_state.py`（v3 §3.5 落地） |

`context_recover` v3（2026-10-28）：state 包含累计待确认候选，其 fact_id 映射到服务端
proposal_id，供后续页显式撤回 / 替换；服务端保持候选待确认，不直接激活恢复事实。

三条硬规则：

1. `task_id` 必须与 `StructuredCaller` 的 `task` 参数、`runs.prompt_versions` 的键、本契约上表三处一致；
2. 新增任务必须**同时**建 prompt 文件、登记本表、在 `runs.prompt_versions` 里出现，缺一不可；
3. 多模态任务同样受本契约约束，差异只在消息组装（见 §四）。

**工具类任务的口径（S7.3）**：`chat_supervisor` 的 prompt 正文里**不写工具清单与参数**
——工具定义（name / description / 参数 schema）是 API 协议，单一来源在 `services/chat_tools.py`
的 `TOOLS`，随 model 的 `tools` 参数下发；prompt 的 03 Constraints 只写"能不能调、边界在哪"。
这样工具改参数不会同时改两份。对话的 prompt 版本记在 `chat_messages.prompt_versions`
（对话消息不写 `runs`）。

**对话工具清单（S7.7 起）**：对话层能调的工具**只有**下表这些。**参数 schema 与返回结构的
单一来源仍是 `services/chat_tools.py` 的 `TOOLS` 与各 handler 的 payload**——本表只登记
"有什么、有没有副作用、用不用 prompt、返回要点"，不复制 schema（复制就会漂移；
`tests/unit/test_prompts_assets.py` 有一条用例钉住"本表的工具名集合 == `TOOLS` 的名字集合"）。

| 工具 | 用途 | 副作用 | 带 prompt 版本 | 返回要点 |
| --- | --- | --- | --- | --- |
| `run_hotspot_analysis` | 把一条热点跑一遍完整分析（拆解 → 检索 → 缺口 → 撰稿） | **有**：投 Celery job、写 `runs` 与 `run_matches` | 间接：落 `runs.prompt_versions` | run_id、候选数、首选素材、覆盖率、初稿标题、成本 |
| `parse_image_clue` | 把本轮图片拆成热点描述 + 爆点要素 | 无（图片只在内存里变成模型输入，不落盘不入库） | `image_hotspot_clue` | 热点描述、要素清单 |
| `search_materials` | 在创作者素材库里检索能蹭这条热点的素材 | 无（只读库） | `hotspot_clue` | 要素、覆盖率、候选摘要、成本 |
| `read_run` | 读回一次已完成运行的结果（`run_id` 可省：默认本会话最近一次运行） | 无（只读库、不调模型、不花钱） | 无 | 每条热点的要素 / 覆盖率 / 候选数 / 首选 / 初稿标题 |
| `preview_element_coverage` | 要素拆解 + 字面通道覆盖估算与补拍建议 | 无（只读库、不调 embedding、不落 `runs`） | `hotspot_clue` | 要素、覆盖率、缺口与补拍建议 |
| `revise_draft` | 按用户要求改写某次运行已产出的初稿（`run_id` 可省：默认本会话最近一次运行） | **有**：花一次模型钱；**不写库**（run 的初稿快照不动） | `copy_revise` | `refused` + 理由，或改后初稿 |

**网页工具的条件式口径（`chat_supervisor` v6 起）**：`search_hotspots` / `fetch_url` 是否随本轮
`tools` 下发由开关 `[web].enabled` 决定（**默认关闭**），而 prompt 只有一份。所以这两条规则同时成立：

1. §02 Task 必须把它们写成**条件式**——「如果本轮工具清单里有搜索 / 读取类工具」才走搜索 / 读取
   这两步，清单里没有就跳过；**禁止**在正文里无条件断言这两个工具存在（关闭态下模型会去调一个
   本轮根本没收到的工具）。跳过后流程必须有出口：没有搜索类工具、用户也没给出具体热点时，问一句
   「你想蹭哪条热点」，不得答成能力范围外；**§03.1 的"不在这套工具的能力范围内"是引号固定话术，
   优先级高于叙述，触发条件又正好是"清单里没有对应工具"，所以必须在那里显式开例外**（「缺一个
   可选工具」不算越界），否则固定话术会反噬 §02 的禁令；
2. §03 的**轮次预算不写死数字**——实际值随开关变（`[chat].max_tool_rounds` 默认 3，网页开启时
   `chat_tools.effective_max_rounds` 至少 8），正文只写「以本轮系统说明为准」。

本口径只**点名**工具，不复制 schema（schema 仍只在 `services/chat_tools.py`）。措辞由
`tests/unit/test_prompts_assets.py::TestSupervisorWebToolWording` 钉住；开关两臂的对照按 §六 的
对话评测（`scripts/eval_chat_tools.py`）跑，不靠单元测试代替真模型效果证据。

**工具类 prompt 的控制字段（S7.8 起）**：工具类 prompt 允许在最外层带**控制字段**——用来表达
"这次不产出领域对象"（例如 `copy_revise` 的 `refused` / `reason`）。领域字段仍必须引用数据契约 /
API 契约里的既有 schema，不得自造字段（§二 05 Output Schema 与 §五 的"不新造字段"说的是这一层）。

**上下文状态抽取（S7.24）**：`context_extract` 是**状态增量**任务，不是对话任务，口径四条：

1. 占位符固定为 `{{ user_text }}`（本轮用户原文，必填）、`{{ state }}`（现有生效 / 待确认事实的
   确定性 JSON 渲染，可为 `[]`）、`{{ refs }}`（服务端已验证的对象引用数组，可为 `[]`）；
2. 输出必须是 JSON 对象 `{operations: [...], proposals: [...]}`：`operations` 只允许
   `add` / `retract` / `replace` 三种，`proposals` 只用于推断、歧义或替换意图不明的冲突；
3. 每条操作必须带 `evidence`（用户原文的**逐字片段**）与 `target_fact_id`（撤回 / 替换时）；
   服务端会做子串匹配与归属校验，匹配不上直接拒绝该条，**不采信模型的置信度字段**；
4. 抽取默认输出上限 2048 tokens（`[context].extract_max_tokens`），结构解析失败最多修复一次
   （`[context].extract_repair_max`），修复也是一次真实请求、逐次计费。prompt 正文里不写
   "你可以确认"这类越权话术——确认只能由用户动作产生。

**机读数据资产（S7.11 起，ADR 0016）**：`src/xhs_agent/prompts/` 除 `<task_id>.md` 外，允许放
**机读数据资产** `<name>.json`，规则三条：① 它不是 prompt 任务（`.json` 不进 `available_tasks()`
的 `.md` 扫描，不受五段结构约束）；② 只能由 `tools/prompt.py` 的读写函数加载，格式不合契约直接抛
`PromptError`；③ 需要模型看到的内容一律经**现有占位符机制**注入到某个 prompt 的 §02 Task
（当前唯一的资产是能力清单 `chat_capabilities.json`，由 `chat_supervisor` 的
`{{ capabilities }}` 注入，组装规则不变）。

**能力清单（S7.11）**：`prompts/chat_capabilities.json` 是越界处理的**唯一来源**，三类边界按固定
顺序 `in_scope`（能力内）/ `out_of_scope_guidable`（边界外可引导）/ `compliance_red_line`（合规红线），
每类含 `title` / `judge` / `reply`（固定话术）。`chat_supervisor` 的 §01 System 声明"边界以本轮
下发的《能力清单》为准"，§03 Constraints 只写"按清单三类分别处理"与红线口径（含**约束作用于用户
指令本身**）；清单正文经 §02 Task 注入。清单与 prompt 的一致性由
`tests/unit/test_prompts_assets.py::TestCapabilityCatalog` 钉住。

## 二、五段结构

一个任务一份文件，文件内固定五段：

| 段 | 职责 | 允许写什么 | 不允许写什么 |
| --- | --- | --- | --- |
| 01 System | 角色与长期规则（跨请求不变） | 身份、服务对象、长期行为准则 | 具体任务步骤、字段名、输出格式 |
| 02 Task | 当前任务 | 本次要做什么、`{{变量}}` 占位符、所需变量声明 | 行为边界、输出字段定义 |
| 03 Constraints | 行为边界 | 见下方四条硬规则 + 任务特定禁令 | 任务步骤、示例 |
| 04 Examples | 好的结果长什么样 | 1–2 个精简输入输出样例 | 引用 `evals/cases/`（避免耦合与超长） |
| 05 Output Schema | 结果有哪些字段 | 字段清单，**引用**数据契约与 API 契约 | 新造字段、重述字段语义 |

**三种表述的对应关系**：常说的「角色规则 / 当前任务 / 约束与输出」是粗分——角色规则 = 01 System，当前任务 = 02 Task，约束与输出 = 03 Constraints ＋ 05 Output Schema；04 Examples 是为了给少样本示范而增加的第五段。

### 03 Constraints 的四条硬规则

**总原则写在段首：不确定的信息，不能写成事实。** 四条规则都要落到可检查的字段要求上，不是口号：

| # | 规则 | 字段级要求 |
| --- | --- | --- |
| 1 | 未知或未经确认的内容必须明确标记 | 无法从输入回溯的结论，要么不输出，要么在 `evidence` 里写明「输入未提供」，并把 `confidence` 压到 ≤ 0.4 |
| 2 | 存在冲突的来源并列说明 | 热点原文与素材标签互相矛盾时，必须在 `why_it_works` / `reasons` 里并列两个来源，不得只取其一 |
| 3 | 事实与推测分开表达 | `why_it_works` 中可回溯的观察与推测性判断必须分开表述，推测需显式标注「推测」 |
| 4 | 每个关键结论保留来源 | 每个 `Element` 必带 `evidence`，且必须指向具体输入（热点原文片段，或素材字段名＋值）；空 `evidence` 视为违规 |

这四条与 `docs/评测集.md` 的「事实准确率 ≥ 90%」「幻觉 0 次」两个维度一一对应，是它们的口径来源。

## 三、文件格式与命名

- 路径：`src/xhs_agent/prompts/<task_id>.md`，随 wheel 分发，用 `importlib.resources` 读取。
- 文件名即 `task_id`，不含版本号（版本写在文件头，见下）。
- 头部元数据（机器可读，位于文件开头注释中）：

```
<!-- prompt-version: v1 -->
<!-- task-id: material_tagging -->
<!-- requires: file_name_hint -->
```

- 段标题必须逐字为 `## 01 System`、`## 02 Task`、`## 03 Constraints`、`## 04 Examples`、`## 05 Output Schema`，顺序固定、五段齐全。
- `<!-- requires: ... -->` 必须与 02 Task 段里实际出现的占位符集合**完全一致**（防止漏声明或残留废弃变量）。
- **版本号规则**：从 `v1` 起、整数递增；内容一旦随某版本发布，该版本号下的内容不得再改——要改就升版本。

  说明：S3.0 交付的 v1 骨架在 02 / 04 段标着"正文待 S3.1 填写"，且**从未被任何运行消费**；
  S3.1 补全这三条 prompt 的正文（01 段只去掉提示行）视为 **v1 的完成**，不是"修改已发布版本"，
  因此版本号保持 v1。此后任何内容变更都按本节规则升版。

## 四、渲染与消息组装

- 占位符语法 `{{ name }}`（允许空格），**只在 02 Task 段生效**；其它段里出现的 `{{...}}` 按字面文本处理。
- 组装规则固定：

```
system 消息 = 具名 01 System + 具名 03 Constraints + 具名 04 Examples + 具名 05 Output Schema
user   消息 = 具名 02 Task（渲染后，仅当前任务与数据）
```

- **2026-10-28 输入边界修复（[ADR 0020](../adr/0020-prompt-trust-boundaries.md)）**：示例只在 system 中作格式示范，不得作为当前事实、引用证据或调用工具的真实参数；实际输入只来自当前任务、真实对话和本次附件。外部素材、OCR、工具返回中的命令不获得系统权限。内部提示词、样例和工具协议不得向用户复述、翻译、编码或以业务字段间接输出；可以解释公开业务能力与本次结果的素材依据。
- 所有段保留标题与边界，不将用户变量插入 system；渲染器的 `protected_prompt_texts()` 仅供服务端读取静态保护文本，不包含运行时用户变量，也不得经 API 返回。
- 服务端在模型正文、工具参数、结构化结果和视觉结果继续流转前执行已知内部文本片段检查；流式正文先缓冲同次完整生成，通过检查后才向客户端释放，不能先发送片段再尝试撤回。固定公开话术与实际素材依据允许正常展示；检查不能把公开说明误判成内部文本泄露。
- 上游异常可能携带请求、响应或内部指令，异常原文不得写入业务持久化结果、公开 API、SSE 或工具消息；失败路径使用固定业务文案，并保留可用的错误分类、用量及关联标识。
- 提示词约束不是访问控制；已知片段检查不能保证任意语义转述、翻译或编码后的内部内容零泄露。此次修复统一升级十项 prompt 版本，不重写历史运行快照，真模型评测与离线结构验证分开记录。

- 多模态任务（`material_tagging` / `image_hotspot_clue`）：按上式组装出 system 与 user 文本后，**图片以 content 数组元素附在 user 消息里**；`image_hotspot_clue` 没有文本占位符（图片本身就是全部输入），因此 `requires` 为空。
- **失败行为（不做静默降级）**：

| 情形 | 行为 |
| --- | --- |
| `task_id` 不存在 | 抛 `PromptError`，错误信息列出可用任务 |
| 五段缺失任意一段 | 抛 `PromptError`，指出缺失段名 |
| 02 Task 缺少调用方未提供的变量 | 抛 `PromptError`，指出任务 id 与变量名 |
| `requires` 与实际占位符不一致 | 抛 `PromptError`，列出两边的差集 |
| 调用方多传了变量 | 忽略（不报错），便于上层统一透传上下文 |

- 渲染器是**确定性能力**，放在 `tools/prompt.py`；不引入任何模板引擎（需要条件、循环时走 `AGENTS.md` 第 6 节流程并补 ADR）。

## 五、版本与落库

- 数据契约 §3.3 的 `runs.prompt_versions`（jsonb，默认 `{}`）记录**本次运行各任务实际使用的版本**：

```json
{"hotspot_clue": 1, "material_select": 1, "copy_draft": 1}
```

- 键为 `task_id`、值为整数版本号；未参与本次运行的任务不出现。
- 该字段是"同一批输入重测"可对比的前提：评测结果必须能追到具体 prompt 版本，否则版本对比无从谈起。
- **A05（2026-10-18）**：`copy_draft` 与 `copy_revise` 升到 **v3**，新增必需变量
  `constraints_json`——服务端筛选的适用事实快照（`fact_id` / `kind` / `value` / `scope` /
  `scope_id` / `source_ref`）。来源分别是 `runs.context_snapshot`（worker 撰稿）与当轮
  会话状态（对话改稿）；两处必须使用同一筛选口径（session 全程 / 当前 topic / 已验证产物），
  不得在代码里临时拼另一份。旧调用方必须同时提供该变量，否则按 §四 缺变量报错。
- **2026-10-28 输入边界修复**：统一升级十项 prompt 版本；该次升级后的资产版本为 `hotspot_clue` v2、`material_select` v2、`copy_draft` v3、`chat_supervisor` v5、`copy_revise` v3、`image_hotspot_clue` v5、`image_hotspot_clue_multi` v4、`material_tagging` v2、`context_extract` v2、`context_recover` v3。历史运行快照保留原版本。
- **2026-11-03 网页工具条件式（`chat_supervisor` v5 → v6）**：§02 Task 第 1 条改成五步决策流程
  （已有热点文本 / 图片 → 直接走原有工具；笼统要热点且清单里有搜索类工具 → `search_hotspots`；
  只有候选摘要或用户贴了链接且清单里有读取类工具 → `fetch_url` 最多 2 次；选定一条 →
  `run_hotspot_analysis`，要微调 → `revise_draft`；结论够用就直接作答），两个网页工具一律带条件；
  §03 新增三条约束（搜索结果只是候选不是事实、通用信息查询不归网页工具、不得自行构造 URL），并把
  轮次预算改成「以本轮系统说明为准」。
  **关闭态必须有落点**：清单里没有搜索类工具、用户又只说了「帮我找热点」没给出具体是哪条时，§02
  要让它按 §03 第 5 条问一句「你想蹭哪条热点」，并明确禁止答成「这件事不在这套工具的能力范围内」
  ——默认关闭下这是最高频的一句输入，缺了这个分支整条决策流程没有出口。同轮还校正了两处措辞：
  「选定一条热点」写明主语（用户点明的 / 用户认可的），以及删掉关闭态下并不存在的"候选"对象。
  审阅又发现 §03.1 的固定话术会反噬 §02 的禁令（同一个触发条件、且引号话术粘性更强），已在该条
  后补"唯一的例外是缺一个可选工具"，让「帮我找热点」继续走问句。
  不涉及占位符、段结构与能力清单，评测按 §六 的对话口径（`scripts/eval_chat_tools.py`）对
  `[web].enabled` 两臂各跑一遍归档。
- 落地路径：列与迁移在 S3.9 完成；**仓储层写入（S3.4a）与 worker 接线（S3.4b）**分别承接——S3.1 已在 state / `RunResult` 里产出该字段，落库由 S3.4a 完成。

## 六、变更流程与准入门槛

**三条维护要求**（同时是提交规范）：

1. **一次只改一层**——单次提交只允许修改五段中的一个段；跨段改动必须拆成多个提交。这样 diff 才能与指标变化一一对应；混着改就无法归因。
   **对话类 prompt 的例外（S7.9 起）**：`chat_supervisor` 的 §01–§03 讲的是同一件事
   （能力边界与工具选择），允许在同一次变更里一并修改；它的回归口径也不是下面那张七维度表
   （那是分析链路的），而是**工具选择评测** `scripts/eval_chat_tools.py`——固定一组消息判定
   "该调的调、不该调的不调"，外加改写路径的越界拒绝，报告按
   `evals/reports/chat-tools-<日期>.md` 归档。
   **S7.11 / S7.12 起扩成三节**：① 工具选择（该调的调、不该调的不调）；② **越界话术**
   （红线实体词不出现、拒绝措辞出现、不调用会产出越界内容的工具；可引导类还必须给出替代路径）；
   ③ **多轮**（假工具 + 真模型：指代承接、不重复付费、历史预算内、关键事实不丢）。
   用例集是 `evals/chat/cases.json`（自带 `version`，当前 `chat-v1`；它不属于 evals v1 冻结资产，
   改动只升这个版本号），判定**全部是确定性规则**，不引入模型评委。**轮级成本门槛 ≤ 0.25 元/轮
   （含一次分析）**——`runs.cost_cny` 仍是"一次分析一行"，轮级成本看 `chat_messages.cost_cny`
   （含图片解析，S7.10 起）。
2. **保留可对比的旧版本**——版本号随内容变更递增；对比报告按 `evals/reports/prompt-<task_id>-v<N>-<日期>.md` 归档入库，报告里写明本次改动的是哪一层、为什么改。
3. **用同一批输入重新测试**——固定使用 `evals/cases/` 的 20 组用例，变更前后各跑一遍，报告列出每条用例的维度得分与总分变化。

**七维度硬门槛**（口径见 `docs/评测集.md`，任一不达标即回退）：

| 维度 | 门槛 |
| --- | --- |
| 幻觉 | 必须为 0 次（新增幻觉即回退） |
| 事实准确率 | 不得低于上一版本 |
| 输出结构 | 全部通过（对齐 API 契约与产品方案 §8.1） |
| Tool 选择 | 100%（该调的调、不该调的不调） |
| 任务完成率 | Top-5 ≥ 80%、首选 ≥ 50%，且不得下降 |
| 成本 | ≤ 0.15 元/热点（ADR 0012 按实测上调） |
| 速度 | ≤ 60 秒/热点 |

**不在门槛内的指标**：产品方案 §10.2 的「文案可用率」需要人工 1–5 分打分，因此不阻断 prompt 变更；待 E3 具备撰稿产出物后一并记录，但不作为回退依据。

## 七、与其它契约的关系

| 契约 / 文档 | 关系 |
| --- | --- |
| `数据契约.md` | 05 Output Schema 的字段落点；`runs.prompt_versions` 的定义方 |
| `openapi.yaml` | 05 Output Schema 对 API 响应的字段口径（`RunDetail` 起） |
| `检索契约.md` | 约束 2「冲突来源并列」在候选解释里的判定口径；本契约不改动检索算法 |
| `配置契约.md` | 无新增配置项：prompt 目录固定在包内，不通过配置切换 |
| `docs/评测集.md` | §六 的七维度门槛与 `evals/cases/` 用例集，是本契约的验证手段 |
| `AGENTS.md` | 目录边界、依赖策略、DoR/DoD 的上位规则 |

## 八、变更规则

1. 本契约是 prompt 资产的唯一事实源；改 prompt 结构或渲染规则必须先改本文件，再改 `tools/prompt.py` 与各 prompt 文件。
2. **破坏性变更**（改 `task_id`、改占位符语义、改段标题、改版本号规则、改组装规则）必须补 `docs/adr/` 一条 ADR，说明迁移方式。
3. 新增或删除任务时，同步更新：§一 的任务表、`runs.prompt_versions` 的键集合、以及调用方。
4. 每次 prompt 内容变更，都要在同一次改动里更新文件头的 `prompt-version`，并按 §六 归档对比报告。
