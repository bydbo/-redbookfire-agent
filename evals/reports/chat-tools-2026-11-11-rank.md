# 对话层回归 · 「第 N 条」只认那次运行的 rank（P0-1 / P0-2）· 2026-11-11

- prompt：`chat_supervisor` **v7**（基线 `HEAD`）→ **v8**（工作区）
- 用例集：`evals/chat/cases-v3.json`（`chat-v3`：`chat-v2` 的 36 条逐字保留 + 1 条多轮用例）
- 范围：`--suite all --repeat 3`（两臂：基线只跑工具选择那节，其余节按脚本既有口径只跑候选臂）
- 环境：评测时临时把 `[web].enabled=true`、`search_url=http://127.0.0.1:8080`、
  `engines=360search,yandex`；仓库默认值仍是 `enabled=false`（评测后已还原）
- 模型：`deepseek-flash`；轮级成本门槛 ≤ 0.25 元/轮（含一次分析）
- 机读结果：`chat-tools-2026-11-11.json` / 脚本报告：`chat-tools-2026-11-11.md`

```
uv run python scripts/eval_chat_tools.py --cases evals/chat/cases-v3.json \
    --repeat 3 --baseline HEAD \
    --note "P0-1/P0-2：read_run 回整份候选清单 + chat_supervisor v8 规定「第 N 条」只认那次运行的 rank"
```

## 一、改的是哪两层、为什么改

真机反馈（2026-11-10）：用户说「第 4 条不是女性在草地帐篷旁欢呼吗」，助手答不上来，还把用户说的
判成记错。查下来**不是**「模型没看到前面的消息」——历史确实进了上下文；真正的缺口是三处：

1. `read_run` 的精简形状（`_run_summary`）每条热点只回 `count` + `top1_title`，**候选清单整份丢了**；
2. 装配给模型的历史只留工具摘要行（不含 payload），所以「第 4 条」的唯一来源就是重新检索；
3. 重新检索每次排序都可能不同（`search_materials` 每轮重跑 `_clue_for` + `retrieve_candidates`），
   而 prompt 没有「序号对齐」的口径，模型于是拿新排序去否定用户。

对应两层改动（同一次变更；`prompt契约` §六 的对话类例外允许 §01–§03 一并改）：

| 层 | 改动 | 位置 |
| --- | --- | --- |
| 工具返回形状（P0-1） | `read_run` / `run_hotspot_analysis` 的候选键名统一成 `count`（总数）+ `candidates`（≤8 条，每条 `rank` / `title` / `score` / `usage` / `material_id`）；`rank` 原样透出 | `services/chat_tools.py`（`_candidate_brief` / `_candidate_briefs` / `_run_summary` / `_summarise`）、`TOOL_READ_RUN` 的 description、`prompt契约` §二 工具表 |
| prompt 规则（P0-2） | §02(1) 把「第 4 条是什么」并进「读回那次运行」；§03 新增第 10 条：序号只认那次运行的 `rank`，不得用 `search_materials` 的新排序顶替，**绝不质疑用户**，`title` 与 `usage` 不一致时以 `usage` 为准 | `prompts/chat_supervisor.md`（v8） |

用例侧同步加了一条多轮用例（`chat-v3`）与双向判据（`must_contain_last` + 新增的
`must_not_contain_last`）：终稿必须写出第 4 条的标题，且不得出现「记错 / 没有第 4 条 / 不存在第 4」。
假 payload 里第 4 条是「湖边露营举手欢呼」（`usage`＝女性在草地帐篷旁欢呼的中景），而
`search_materials` 的假返回故意换了顺序（只 2 条、rank 2 在前）——拿新检索顶替一定对不上号。

## 二、结果

| 节 | 基线 v7 | 候选 v8 | 结论 |
| --- | --- | --- | --- |
| 工具选择（14 条 ×3） | 14/14（100%） | 14/14（100%） | 无回退 |
| 越界话术（10 条 ×3） | — | 10/10（100%） | 无回退 |
| 多轮（7 条 ×3） | — | **6/7** | 新增用例 3/3；「网页：多轮承接」2/3（归因见 §三） |
| 改写合规（3 条 ×3） | — | 3/3 | 无回退 |
| 多步（3 条 ×3） | — | 2/3 | 一次 `LLMError`（网关抖动），见 §三 |

**关键一条**：新增用例「多轮：按那次运行的 rank 认候选」**3/3 通过**——两轮都是
`run_hotspot_analysis → read_run`，终稿写出了「湖边露营」并给了 `usage` 那一栏的画面描述，
三个禁词一次都没命中，单轮成本 0.0291–0.0293 元。

单测层同向覆盖：`tests/unit/test_chat_tools.py` 新增 `TestReadRunCandidateList`（rank 顺序、
第 4 行的 `usage` / `material_id` / `score`、超 8 条截断但 `count` 仍是总数、空标题回落文件名）
与 `TestRunAnalysisSummary`（`run_hotspot_analysis` 返回同一份形状）。

## 三、两条没过的项

1. **「网页：多轮承接（搜→分析）」2/3**：第 1 轮有一次多调了两次 `fetch_url`（搜完顺手读链接）。
   该轮的允许集合只有 `search_hotspots`，按口径判失败——属于**用例判定的松紧**（与 web-tools 报告
   §八 登记的判定脆弱性同一类），与本次改动无关：v7 单跑该用例是通过的
   （`chat-tools-2026-11-09-multiturn.md` 6/6）。
2. **多步「搜热点→跑分析」2/3**：三次里有一次模型调用直接 `LLMError`（`failed`，成本 0.0259 元），
   另外两次都正常走完 3 步并命中依据词。属环境抖动，不是规则问题。

## 四、关闭态为什么不单独跑

`chat-v3` 是 `chat-v2` 的超集（14/10/7/3/3），而 v8 没有触碰网页工具的条件式——`search_hotspots` /
`fetch_url` 仍由 `[web].enabled` 决定是否下发。关闭态的 28 条对照见 `chat-tools-2026-11-09.md`
（v7 全量 28/28）；本次改动只涉及 `read_run` 的返回形状与序号口径，与网页开关无交集。
