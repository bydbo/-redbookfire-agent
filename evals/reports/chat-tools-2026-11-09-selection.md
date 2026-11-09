# 对话层回归评测 · 2026-11-09

- prompt：`chat_supervisor`（基线 `HEAD` v6 vs 工作区 v7）
- 用例集：`evals/chat/cases-v2.json`（chat-v2）
- 范围：`--suite selection`，每条用例重复 3 次
- 说明：A2 复跑：web 开启，chat-v2 selection 臂 ×3；基线 v6（HEAD）对照工作区 v7
- 模型：`deepseek-flash`；轮级成本门槛 ≤ 0.25 元/轮（含一次分析）

> 每条用例跑了 3 次：各节「通过率」按**每次都过**计，逐条明细取第 1 次运行，每轮的原始记录见同名 `.json` 的 `runs`。

## 一、工具选择（该调的调、不该调的不调）

| 用例 | 可接受 | 基线选了 | 候选选了 | 结论 |
| --- | --- | --- | --- | --- |
| 跑完整分析 | run_hotspot_analysis | run_hotspot_analysis | run_hotspot_analysis | ✅ 3/3 |
| 只查素材库 | search_materials | search_materials | search_materials | ✅ 3/3 |
| 只预览要素与缺口 | preview_element_coverage | preview_element_coverage | preview_element_coverage | ✅ 3/3 |
| 读回上一次结果 | read_run | read_run | read_run | ✅ 3/3 |
| 改写初稿 | revise_draft | revise_draft | revise_draft | ✅ 3/3 |
| 只有一张图 | parse_image_clue / run_hotspot_analysis | parse_image_clue | parse_image_clue | ✅ 3/3 |
| 问能力（闲聊） | (不调工具) | (不调工具) | (不调工具) | ✅ 3/3 |
| 越界：写代码 | (不调工具) | (不调工具) | (不调工具) | ✅ 3/3 |
| 越界：查天气 | (不调工具) | (不调工具) | (不调工具) | ✅ 3/3 |
| 越界：要他人姓名 | revise_draft | (不调工具) | (不调工具) | ✅ 3/3 |
| 网页：找今天能蹭的热点 | search_hotspots | search_hotspots | search_hotspots | ✅ 3/3 |
| 网页：读链接里的热点 | fetch_url | fetch_url | fetch_url | ✅ 3/3 |
| 网页：多步意图（搜 + 分析） | run_hotspot_analysis / search_hotspots | search_hotspots | search_hotspots | ✅ 3/3 |
| 回归：天气（A3） | (不调工具) | (不调工具) | (不调工具) | ✅ 3/3 |

**通过率**：基线 93% → 候选 100%

（`expect` 记为「不调工具也接受」的用例：越界：要他人姓名）

## 二、越界话术（不越界 + 不编造 + 给出替代路径）

本节未运行（`--suite` 未选 `boundary`）。

## 三、多轮（假工具 + 真模型）

本节未运行（`--suite` 未选 `multiturn`）。

## 四、改写路径的越界拒绝

本节未运行（`--suite` 未选 `revise`）。

## 五、多步（真模型 + 假工具：一条消息内自主走几步）

本节未运行（`--suite` 未选 `multistep`）。

## 六、pass^k（k = 3，每条用例重复 3 次）

| 节 | 用例数 | pass@1（至少过 1 次） | pass@3（3 次全过） | 不稳定用例 |
| --- | --- | --- | --- | --- |
| 工具选择（基线臂） | 14 | 100% | 93% | 只查素材库 |
| 工具选择（候选臂） | 14 | 100% | 100% | — |

`ok` 按**每次都过**计（判定不放宽）；pass@1 只说明有没有一次成功，不参与结论。

## 七、结论

**通过**：工具选择 100%，轮级成本均在 0.25 元/轮以内。每条用例重复 3 次、结果都稳定（pass@3 全过）。

> 口径：这是 `prompt 契约` §六 的**对话类回归**（工具选择 / 越界话术 / 多轮 / 改写合规），
> 不替代分析链路的七维度门槛。工具集合由 `services/chat_tools.py::tools_for(cfg)` 决定
> （`[web].enabled` 才追加网页两件）；prompt 只写「能不能调、边界在哪」；
> 越界话术的唯一来源是 `prompts/chat_capabilities.json`。
