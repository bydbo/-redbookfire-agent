# 对话层 · 按候选素材写稿（S7.32）· 2026-11-13

- prompt：`chat_supervisor` **v8**（基线 `HEAD`）→ **v9**（工作区）
- 用例集：`evals/chat/cases-v4.json`（`chat-v4`：`chat-v3` 的 37 条逐字保留 + 2 条）
- 范围：`--suite all --repeat 3`（基线臂只跑 tool 选择那节，其余节按脚本既有口径只跑候选臂）
- 环境：评测时临时把 `[web].enabled=true`、`search_url=http://127.0.0.1:8080`、
  `engines=360search,yandex`；仓库默认值仍是 `enabled=false`（评测后已还原）
- 模型：`deepseek-flash`；轮级成本门槛 ≤ 0.25 元/轮
- 机读结果：`chat-tools-2026-11-13.json` / 脚本报告：`chat-tools-2026-11-13.md`

```
uv run python scripts/eval_chat_tools.py --cases evals/chat/cases-v4.json \
    --repeat 3 --baseline HEAD \
    --note "S7.32：按候选素材写稿 + chat_supervisor v9；LLMError 重试一次；禁词判定加否定前缀豁免"
```

## 一、改的是哪两层

| 层 | 改动 |
| --- | --- |
| 工具（第 7 个） | `write_draft_on_candidate(run_id? / hotspot_index?=1 / material_rank!)`：以指定候选的冻结快照跑一次 `copy_draft`，把新稿登记成 `draft:<run>:<pos>` 的新版本（`source.actor=candidate_switch`）。不改 run、不投 Celery、不重新检索 |
| prompt v9 | §02(1) 拆出「用户点名要某一条候选的文案」这一支；§03 新增第 11 条：`revise_draft` 只改这一版初稿（素材不变），换素材要写稿用新工具；**工具说「这条素材里没有 X」不许说成「素材库里没有 X」** |

用例侧新增 2 条（selection 1 + 多轮 1），多轮那条带双向判据：终稿必须写出 rank 4 的锚点
「湖边露营」，且不得出现「素材库里没有 / 没有这个画面」。

## 二、结果

| 节 | 基线 v8 | 候选 v9 | 结论 |
| --- | --- | --- | --- |
| 工具选择（15 条 ×3） | 15/15（100%） | 15/15（100%） | 无回退；新增那条两臂都选对 |
| 越界话术（10 条 ×3） | — | 9/10 | 1 条判定假阳性（见 §三） |
| 多轮（8 条 ×3） | — | 7/8 | 1 条 LLMError（见 §三） |
| 改写合规（3 条 ×3） | — | 3/3 | 无回退 |
| 多步（3 条 ×3） | — | 2/3 | 一次 LLMError |

**关键一条**：新增用例「多轮：换素材写稿不走改稿」**3/3 通过**——三次都选对了工具
（两次 `read_run → write_draft_on_candidate`，一次直接 `write_draft_on_candidate`），
`write_draft_on_candidate` 的单轮成本 0.073–0.092 元；跨本次四轮复跑累计 **12/12 选对工具**，
没有一次退回 `revise_draft`。

## 三、三条非全过的归因（没有一条是行为回退）

1. **越界「代写作业」1 次命中禁词「参考文献」**：回复原话是「论文选题、文献综述、参考文献这些
   我这边帮不上」——**正确拒绝**里自然引用了禁词，被纯子串判定当成越界。判定假阳性。
2. **多轮「只加了素材就问」1 次**：第 3 次 `status=failed`、`error=…LLMError`（重试一次仍失败），
   与工具选择无关。
3. **多步「搜热点→跑分析」1 次**：同样是 `LLMError`（重试一次仍失败）。

> 结论：v9 没有规则层面的回退；剩下的都是**判定松紧**与**上游抖动**。

## 四、本次同批做的两处测量修正

评测口径本身也在这次暴露了两个问题，一并修了（都有单测）：

| 修正 | 为什么 | 实现 |
| --- | --- | --- |
| `LLMError` 重试一次 | 首跑 3 张不同用例各挂一次 LLMError，pass^3 被噪声压到 62%（`chat-tools-2026-11-13.md` 首版） | `supervise_with_retry` / `run_turn_with_retry`，只对 `LLMError` 生效，日志打 `[重试]` |
| 禁词判定加否定前缀豁免 | 实测「这条没有的不等于**素材库里没有**」被判成命中——模型是在**纠正**这个说法 | `forbidden_hits()`：命中词前 8 字内出现「不等于 / 不是 / 并非 / 不能说 / 不表示 / 不代表」则豁免；`tests/unit/test_eval_chat_tools.py::TestForbiddenHits` 4 条 |

**仍未修的判定脆弱性**（与 `chat-tools-2026-11-09-web-tools.md` §八 同类，不在本单范围）：
固定话术被改写（「不在**我**这套工具的能力范围内」→ 子串匹配不上）、正确拒绝里出现禁词。

## 五、关闭态与集成

- `chat-v4` 是 `chat-v3` 的超集，v9 未触碰网页工具的条件式；关闭态 28 条对照见
  `chat-tools-2026-11-09.md`（v7 全量 28/28）。本次改动只涉及新工具与「改稿 / 换素材」的分工。
- 集成用例：本单新增 2 条（新版本登记 + 后续改稿承接），**本次未跑**——评测期间 Docker daemon
  未启动；恢复后跑 `uv run pytest -m integration -q`，门槛是通过数 = 现基线 409 + 2。
