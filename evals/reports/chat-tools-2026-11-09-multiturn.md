# 对话层回归评测 · 2026-11-09

- prompt：`chat_supervisor`（工作区 v7；基线臂未跑）
- 用例集：`evals/chat/cases-v2.json`（chat-v2）
- 范围：`--suite multiturn`，每条用例重复 1 次
- 说明：A2 补充：web 开启，chat-v2 multiturn 节（含移入的「网页：多轮承接」用例）
- 模型：`deepseek-flash`；轮级成本门槛 ≤ 0.25 元/轮（含一次分析）

## 一、工具选择（该调的调、不该调的不调）

本节未运行（`--suite` 未选 `selection`）。

## 二、越界话术（不越界 + 不编造 + 给出替代路径）

本节未运行（`--suite` 未选 `boundary`）。

## 三、多轮（假工具 + 真模型）

| 用例 | 逐轮工具选择 | 缺要点 | 历史预算 | 成本（元） | 结论 |
| --- | --- | --- | --- | --- | --- |
| 多轮：图片指代承接 | parse_image_clue → run_hotspot_analysis | — | — | 0.0340 / 0.1140 | ✅ |
| 多轮：结果追问不重复分析 | run_hotspot_analysis → read_run | — | — | 0.1184 / 0.0283 | ✅ |
| 多轮：改写承接上一次运行 | run_hotspot_analysis → revise_draft | — | — | 0.1185 / 0.0463 | ✅ |
| 多轮：长历史仍在预算内 | run_hotspot_analysis | — | 5998/6000，裁掉 24 条 | 0.1531 | ✅ |
| 多轮：只加了素材就问 | run_hotspot_analysis → search_materials | — | — | 0.1155 / 0.0305 | ✅ |
| 网页：多轮承接（搜→分析） | search_hotspots → run_hotspot_analysis | — | — | 0.0289 / 0.1256 | ✅ |

**通过率**：100%

## 四、改写路径的越界拒绝

本节未运行（`--suite` 未选 `revise`）。

## 五、多步（真模型 + 假工具：一条消息内自主走几步）

本节未运行（`--suite` 未选 `multistep`）。

## 六、pass^k（k = 1，本次未重复运行）

| 节 | 用例数 | pass@1（至少过 1 次） | pass@1（1 次全过） | 不稳定用例 |
| --- | --- | --- | --- | --- |
| 多轮 | 6 | 100% | 100% | — |

`--repeat 1`：两个指标都是同一次运行的结果，等价于改造前的通过率。

## 七、结论

**通过**：多轮 6/6，轮级成本均在 0.25 元/轮以内。

> 口径：这是 `prompt 契约` §六 的**对话类回归**（工具选择 / 越界话术 / 多轮 / 改写合规），
> 不替代分析链路的七维度门槛。工具集合由 `services/chat_tools.py::tools_for(cfg)` 决定
> （`[web].enabled` 才追加网页两件）；prompt 只写「能不能调、边界在哪」；
> 越界话术的唯一来源是 `prompts/chat_capabilities.json`。
