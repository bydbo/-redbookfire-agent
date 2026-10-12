# ADR 0016 · `prompts/` 携带机读数据资产（能力清单）

- 状态：已采纳
- 日期：2026-10-12
- 关联：`src/xhs_agent/prompts/chat_capabilities.json`、`src/xhs_agent/tools/prompt.py`、
  `src/xhs_agent/prompts/chat_supervisor.md`、`docs/contracts/prompt契约.md`（S7.11）

## 背景

S7.11 要求「越界处理」有三类边界（能力内 / 边界外可引导 / 合规红线），每类有**固定话术**，
并且清单本身要**可机读**——评测脚本要按类别归类用例、单测要钉住话术没被随手改掉，
同时模型也必须真的看到这份清单（否则话术只是文档）。

约束：`prompts/` 是 prompt 资产的唯一来源（AGENTS.md 第 1 节），`tools/prompt.py` 的
`available_tasks()` 按 `*.md` 扫描并把每个文件当"五段结构的 prompt 任务"解析；测试还钉住
"任务集合 == 契约表格"。

## 备选方案

| 方案 | 代价 |
| --- | --- |
| 话术手写进 `chat_supervisor.md` §03，另设单测镜像 | 零新增文件；但同一份话术存在两处，改一处忘一处只能靠测试兜，且评测脚本要正则解析 Markdown 才能分类 |
| 把清单做成 `prompts/chat_capabilities.md`（五段 prompt 资产） | 会被 `available_tasks()` 当成一个任务，破坏"任务集合 == 契约表格"；机器可读性也差（要解析 Markdown） |
| **在 `prompts/` 放机读数据资产（JSON），渲染进 §02 Task** | 需要给 prompt 目录引入第二种文件类型；但清单只有一份、测试与模型读同一份，且不碰五段结构与组装规则 |

用 YAML 更符合"配置型数据"的直觉，但 **pyyaml 是 dev 依赖**（AGENTS.md 第 4 节：只给 CI workflow
的静态校验用例用），运行时 import 会让镜像里缺包——按"零新增运行时依赖"的口径改成 **JSON**（标准库即可解析）。

## 决策

`src/xhs_agent/prompts/` 允许携带**机读数据资产** `<name>.json`，规则三条：

1. 它不是 prompt 任务：以 `.json` 结尾，不进 `available_tasks()` 的 `.md` 扫描，
   不受"五段结构"约束；
2. 只能由 `tools/prompt.py::load_capabilities()` / `render_capabilities()` 读写，
   格式不合契约直接抛 `PromptError`（不静默降级）；
3. 需要模型看到的内容，经**现有占位符机制**注入到某个 prompt 的 §02 Task
   （当前是 `chat_supervisor` 的 `{{ capabilities }}`）——组装规则（system = 01+03、
   user = 02+04+05）不变。

## 后果

**正面**

- 三类边界与固定话术只有一份来源：模型看到的就是测试与评测脚本读的那份；
- 零新增依赖（`json` 是标准库），离线可测；
- 组装规则与五段结构不受影响，`chat_supervisor` 只是多了一个声明在 `requires` 里的变量。

**负面**

- `prompts/` 目录不再是"清一色 Markdown"，读目录的人需要知道有第二种文件类型
  （AGENTS.md 第 1 节的目录表与 prompt 契约 §一 已写明）；
- 新增一类资产就要补一条格式校验，否则坏文件要到运行时才炸。

**边界**

- 这条只放开"同目录放机读数据"这一件事，**不放宽**"prompt 文本禁止内联进代码，也不引模板引擎"；
- 需要条件 / 循环的渲染仍走 AGENTS.md 第 6 节流程并补 ADR。
