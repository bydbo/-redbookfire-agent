# 对话层网页工具（search_hotspots / fetch_url）· PR4 真模型评测报告

- 日期：2026-11-09
- 模型：`deepseek-flash`（`config/config.toml`）
- prompt：`chat_supervisor` **v7**（工作区）；基线臂 = `HEAD`（`a32a6f6`）的 **v6**
- 用例集：`evals/chat/cases.json`（`chat-v1`，28 条）/ `evals/chat/cases-v2.json`（`chat-v2`，36 条）
- 环境：评测时临时把 `[web].enabled=true`、`search_url=http://127.0.0.1:8080`、
  `engines=360search,yandex`；仓库默认值仍是 `enabled=false`（评测后已还原）
- 原始记录：同目录下 `chat-tools-2026-11-09*.json`（每条用例的逐次运行、工具链、成本都在里面）

## 一、结论

| 验收 | 目标 | 实测 | 判定 |
| --- | --- | --- | --- |
| **A1** 默认关闭不回归 | `enabled=false` 时 chat-v1 28/28 | 28/28（`chat-tools-2026-11-09.json`）；工具选择两臂 10/10；与开启态共有的 10 条选择、5 条多轮逐条一致 | ✅ |
| **A2** 自主多步 | 多步用例「自主 ≥2 步」≥ 70%；pass@3 ≥ 50% | **100%**（9/9 次运行、3/3 用例都 ≥2 步）；**pass@3 100%**（`-multistep.json`） | ✅ |
| **A3** 天气不越界 | 「明天上海天气」0 工具 + 边界说明 | 开启态 3/3、关闭态 1/1 都是 0 工具并说明边界 | ✅ |
| **A6** 质量门 | ruff / mypy / pytest 全绿 | 见 §七（提交后复跑） | ✅ |

一句话：**默认关闭行为未变；开启后模型能在一条用户消息里自主走完「搜索 → 读正文 → 跑分析」，
且 3 次重复全部稳定；成本每轮远低于 0.25 元门槛。**

## 二、A1：关闭态 chat-v1 全量（28 条）

命令（`[web].enabled=false`）：

```
uv run python scripts/eval_chat_tools.py --suite all --repeat 1 \
    --note "A1：web 关闭，chat-v1 全量 28 条；基线 v6（HEAD）对照工作区 v7"
```

结果（`chat-tools-2026-11-09.md/.json`）：工具选择 100%、越界话术 10/10、多轮 5/5、改写合规 3/3。
选择臂两臂都是 10/10——v7 修掉了 v6 在「只查素材库」上的回退（v6 首跑 9/10，见 §五）。

**首跑 27/28 的说明（如实记录）**：同命令第一次跑时「可引导：代写作业」被判失败，原因是
`must_not_contain` 命中「参考文献」——但回复是标准拒绝：

> 这件事不在这套工具的能力范围内——我这边不做论文、作业这类长篇写作，**也没有参考文献可以给你**。……

它没有调工具、也给出了替代路径，属于**判定误报**（拒绝话术自然引用了用户原话，和用例集里
「官方联名款」那次是同一类假阴性）。复跑 28/28 通过；这个判定脆弱性登记在 §八，不在本次改动范围。

## 三、A2：开启态多步（主线）

### 3.1 工具选择 ×3（`chat-tools-2026-11-09-selection.md/.json`）

```
uv run python scripts/eval_chat_tools.py --cases evals/chat/cases-v2.json \
    --suite selection --repeat 3 --baseline HEAD
```

| 臂 | 用例数 | pass@1 | pass@3 | 不稳定用例 |
| --- | --- | --- | --- | --- |
| 基线 v6 | 14 | 100% | **93%** | 只查素材库（3 次里 1 次误选 `run_hotspot_analysis`） |
| 候选 v7 | 14 | 100% | **100%** | — |

候选臂 14 条全部 3/3：`run_hotspot_analysis` / `search_materials` / `preview_element_coverage` /
`read_run` / `revise_draft` / `parse_image_clue` / 闲聊 / 三类越界 / 网页找热点 / 网页读链接 /
多步意图首跳 / **A3 天气 0 工具**。

### 3.2 多步节 ×3（`chat-tools-2026-11-09-multistep.md/.json`）

```
uv run python scripts/eval_chat_tools.py --cases evals/chat/cases-v2.json \
    --suite multistep --repeat 3
```

一条用户消息，模型自己决定走几步；假工具返回固定 payload（搜索 → 夜跑球鞋候选、抓取正文含
「湿滑路面」、分析首选「球拍特写」），只花模型调用钱、不联网。判定 = 步数 ≥2 + 首跳符合 +
终稿含工具返回里的锚点词 + 单轮成本 ≤0.25 元。

| 用例 | 工具链（3 次） | 步数 | 依据锚点 | 单轮成本（元） |
| --- | --- | --- | --- | --- |
| 多步：搜热点→跑分析 | `search_hotspots → fetch_url → run_hotspot_analysis` ×3 | 3 | 夜跑球鞋开箱 / 球拍特写 | 0.1433 / 0.1460 / 0.1489 |
| 多步：搜热点→读正文 | `search_hotspots → fetch_url` ×3 | 2 | 湿滑路面 / 夜跑球鞋开箱 | 0.0377 / 0.0382 / 0.0385 |
| 多步：读链接→跑分析 | `fetch_url → run_hotspot_analysis` ×3 | 2 | 球拍 | 0.1271 / 0.1272 / 0.1319 |

**自主 ≥2 步：100%**（门槛 70%）；**pass@3：100%**（门槛 50%）；9 次运行全部 `succeeded`，
无一次漂到不相关工具。

### 3.3 多轮承接（`chat-tools-2026-11-09-multiturn.md/.json`，web 开启）

6/6 通过，其中 5 条是 chat-v1 原用例（逐条行为与关闭态一致），第 6 条是本次修正后移入多轮节的
「网页：多轮承接（搜→分析）」：第 1 轮 `search_hotspots`，第 2 轮 `run_hotspot_analysis`，两轮都
落在预期工具上（原先把这条放在单轮 selection 里，A2 首跑 0/3——单轮没有「刚才」的上下文，不调
工具是合理行为）。

## 四、与 chat-v1 的逐条差异

- **共有的 10 条工具选择用例**：开启态与关闭态结果**逐条相同**（工具、通过与否都一致）。
- **共有的 5 条多轮用例**：逐轮工具选择与通过结果完全相同。
- **新增 4 条选择用例**：找热点 → `search_hotspots`；读链接 → `fetch_url`；多步意图 → 首跳
  `search_hotspots`；A3 天气 → 0 工具。4/4 通过（×3）。
- **新增 3 条 multistep**：见 §3.2，100% 自主多步。
- 差异只出现在「新增用例」，老用例的判定没有放宽，也没有改口径。

## 五、prompt v6 → v7 的回退修复

A2 首跑（2026-11-03，v6）发现：开启网页后「只查素材库」（我库里有没有能用的素材？热点是…）
被完整分析吸走，v6 候选 0/3、基线 v5 3/3，导致工具选择 93% → 87%。v7 在 §02 第 (1) 步恢复
「先看用户是不是只要某一个中间产物」的选择语义，并把 (4) 收窄为「完整产出且已定下一条热点」。

本次实测：同一用例候选 v7 **3/3**，基线 v6 **2/3**（1 次仍误选 `run_hotspot_analysis`），
selection pass@3 由 93% 回到 100%（§3.1）。

## 六、成本

| 项 | 单轮/单次 | 说明 |
| --- | --- | --- |
| multistep（3 条 ×3 次） | 0.0377–0.1489 元 | 合计 0.3114 元，全部 < 0.25 元门槛 |
| 多轮（6 条，web 开启） | 0.0289–0.1531 元 | 合计 0.9131 元 |
| 多轮（5 条，web 关闭） | 0.0216–0.1529 元 | 合计 0.7325 元 |

**未计入**：工具选择 / 越界话术 / 改写三节走的是 `supervise` 与 `revise_draft` 直调，当前脚本
没有给它们挂账本（历史报告同样如此），所以这三节的成本无法从报告 JSON 读出——这是既有口径，
不是本次新增的空白。

## 七、质量门

- `uv run ruff check .`：All checks passed
- `uv run mypy`：Success，86 source files
- `uv run pytest -q`：**1808 passed / 3 skipped / 402 deselected / 0 failed**（78.19s；
  基线 1588 → PR1–3 后 1797 → 本报告的多步/渲染用例 +11 = 1808）

## 八、已知限制与风险（必须如实读）

1. **判定误报**：首跑 A1 的「代写作业」因拒绝话术引用「参考文献」被判失败（§二）。用例集
   `chat-v1` 已归档、按仓库规则不原地改；建议下一次升版时把该禁词换成产出型标记
   （如「参考文献：」）或按「拒绝引用用户原话」整类处理。
2. **工具选择/越界/改写的成本未记账**（§六）。
3. **402 条 integration 未执行**：Docker 容器侧行为未验证。
4. **DNS rebinding 不防**：D7=A，只拦解析后的私网地址，不钉 IP；https 钉 IP 的 SNI/证书行为未测。
5. **引擎可用性只是本机网络快照**：`360search,yandex` 组合本次 15 条、~1.4s；换网络/换实例
   完全不同，不得当成 SearXNG 的通用能力。`publishedDate` 仍是「键存在、无值时为 null」。
6. `chat-v2` 的 3 条网页用例依赖 `[web].enabled=true`；用 v2 在关闭态跑会看到 3 条必然红——
   那是用例与开关不匹配，不是回退。
7. **按用例单独设 repeat 未实现**：计划 Task 13 写「天气那条重复 1 次」，脚本的 `--repeat`
   是全局开关，实际 selection 14 条都跑了 3 次（天气那条 0 工具调用，重复成本可忽略）。
8. **开启态只单跑了受开关影响的三节**（selection / multiturn / multistep）；boundary 与 revise
   两节与 `[web]` 开关无关、用例与 chat-v1 逐字相同，由 A1 的关闭态 28/28 覆盖。

## 九、复现

```bash
# A1（关闭态；先把 config/config.toml 的 [web].enabled 改回 false）
uv run python scripts/eval_chat_tools.py --suite all --repeat 1

# A2（开启态；[web].enabled=true、search_url=http://127.0.0.1:8080、engines=360search,yandex）
uv run python scripts/eval_chat_tools.py --cases evals/chat/cases-v2.json \
    --suite selection --repeat 3 --baseline HEAD
uv run python scripts/eval_chat_tools.py --cases evals/chat/cases-v2.json \
    --suite multistep --repeat 3
uv run python scripts/eval_chat_tools.py --cases evals/chat/cases-v2.json --suite multiturn
```

> 口径：这是 `prompt 契约` §六 的**对话类回归**（工具选择 / 越界话术 / 多轮 / 改写合规 / 多步），
> 不替代分析链路的七维度门槛。工具集合由 `services/chat_tools.py::tools_for(cfg)` 决定；
> 越界话术的唯一来源是 `prompts/chat_capabilities.json`。
