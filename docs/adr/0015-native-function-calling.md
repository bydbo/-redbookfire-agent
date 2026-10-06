# ADR 0015 · 对话层用原生 function calling

- 状态：已采纳
- 日期：2026-10-06
- 关联：`src/xhs_agent/tools/llm.py`、`docs/backlog.md`（S7.1）、`docs/产品方案.md` §11.2

## 背景

E7 第一批要让「模型自己决定调哪个工具」。backlog 的 S7.1 写了 **DoR 前置**：先用一条最小请求验证
`[llm].model`（本项目是 DeepSeek `deepseek-flash`）端点是否支持 `tools` / `tool_calls`；
支持就走原生，不支持就退到「结构化 JSON 协议模拟」（模型输出 `{tool, args}`，复用 `json_mode`）。

## 实测（2026-10-06，一条最小请求 + 一条流式请求）

- 请求带 `tools` + `tool_choice="auto"`：返回 `finish_reason=tool_calls`，
  `message.tool_calls[0].function` 里有 `name` 与 JSON 字符串 `arguments`；
- 响应带标准 `usage`（prompt / completion tokens），成本可以按 `[llm]` 的单价直接算；
- `stream=True` 可用：一次 60 token 的回复拿到 62 个 `data:` 分片，分片里同样能带 `tool_calls`
  增量（`index` + `function.arguments` 片段）。

## 备选方案

| 方案 | 代价 |
| --- | --- |
| 结构化 JSON 协议模拟 | 零依赖、任何端点都能跑；但要把工具清单塞进 prompt、自己解析 `{tool, args}`、还要处理"模型没按格式输出"的自修——多一层不可靠的解析，且拿不到标准的工具增量 |
| **原生 function calling** | 依赖端点支持（实测支持）；工具清单走 API 参数，`tool_calls` 结构标准，可以边流式边收工具参数增量 |
| 换一个支持 tools 的模型 | 没必要：现役模型就支持，换模型的评测与成本代价远大于收益 |

## 决策

对话层用**原生 function calling**：`LLMCall.tools` + `BaseProvider.complete_with_tools()`；
`stream=True` 时逐分片回吐正文（对话层转成 `text_delta`）并按 `index` 拼接
`tool_calls.arguments`。记账口径与普通调用完全一致（token / 成本 / attempts / latency）。
**不做**结构化 JSON 协议模拟这条降级路径（ADR 0001：不做运行时降级）。

## 后果

**正面**

- 工具定义是 API 参数而不是 prompt 文本，模型选择有标准结构；
- 真 token 流式可用，对话层不需要额外机制就能做"逐字回显"；
- 零新增依赖（httpx 已支持流式读），成本口径与既有 `complete` 共用一份实现。

**负面**

- 这条能力绑在「端点支持 tools」上：将来换模型要先跑一遍 DoR 前置的那条最小请求；
- 流式解析要多写一点（分片拼接、`usage` 可能只出现在最后一片）。

**边界（写进代码注释与开发规范）**

- 流式**已经开始回吐正文后失败不重试**——重试会让用户看到重复文本；改为把半截文本连同
  `error` 一起返回，由对话层决定怎么收尾；
- 400 / 401 / 403 不重试（与既有口径一致），5xx 与网络错误仍按 `max_retries` 退避重试。
