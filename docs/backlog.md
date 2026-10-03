# Backlog 与迭代计划

> 来源：`docs/技术栈.md` 第四节（P1–P4）。本文件把它拆成可执行的 Epics → Stories → Tasks。
> 任务开工与完成的判定标准见 `AGENTS.md` 第 10 节（DoR / DoD）。

## 一、使用说明

- 单位：**人日**，按 1 人日 = 4–6 小时有效开发折算。
- 状态：⬜ 未开始 ／ 🔄 进行中 ／ ✅ 已完成。
- 「依赖」列为 Story 编号；依赖未满足即不满足 DoR，不得开工。
- 每个 Story 都必须能独立验收——验收标准写的是**可观察的结果**，不是"完成了某个动作"。

## 二、Epic 总览

| Epic | 目标 | 人日 | 状态 | 里程碑 |
| --- | --- | --- | --- | --- |
| E0 契约与决策 | 把方案变成可开工的契约与决策记录 | 3.5 | ✅ 已完成 | M1 |
| E1 工程骨架 | 建立契约与质量基线，行为不变 | 7.5 | ✅ 已完成 | M2 |
| E2 数据与检索 | 持久化 + 语义召回，产出评测数字 | 20 | 🔄 进行中 | M3 |
| E3 编排与服务 | 服务化与异步，端到端可演示 | 20.5 | 🔄 进行中 | M4 |
| E4 可观测与交付 | 可运维、可交付 | 7.5 | ⬜ 未开始 | M5 |
| E5 前端工程 | 把演示页升级为可交互的单页应用 | 17 | ⬜ 未开始 | M6 |
| **合计** | | **76** | | |

全职投入约 9 周；按每天 3 小时的业余节奏约 5 个月。总量比初版（41.5）增加 26 人日：前端 Epic 17、API 前缀与静态挂载 1、完整 preflight 1.5、mypy 收紧 0.5、E2 净增 5（集成测试基座 +3、评测集与示例素材包 +3、对比脚本精简 −1）、E1 落地时的范围调整 1（S1.2 配置段全量 +0.5、S1.4 覆盖 8 个模块 +0.5）。

## 三、Story 清单

### E0 · 契约与决策（已完成）

| 编号 | Story | 产出 | 验收标准 | 人日 | 状态 |
| --- | --- | --- | --- | --- | --- |
| S0.1 | 契约文档包 | `docs/contracts/` 四份 | OpenAPI 通过规范校验；数据契约细到可直接写迁移；配置契约每项都有失败行为 | 2 | ✅ |
| S0.2 | 首批 ADR | `docs/adr/` 九条（0001–0009）+ 索引 | 每条含背景、备选、决策、后果 | 1 | ✅ |
| S0.3 | Backlog 与 DoD | 本文件 + `AGENTS.md` 第 10 节 | 每个 Story 带依赖与验收标准 | 0.5 | ✅ |

### E1 · 工程骨架

| 编号 | Story | 依赖 | 验收标准 | 人日 | 状态 |
| --- | --- | --- | --- | --- | --- |
| S1.1 | Pydantic v2 契约重构 | S0.1 | `schemas.py` 的 dataclass 全部升级为 Pydantic 模型；`reasons` 非空约束在模型层生效；`tools/` 行为不变 | 2 | ✅ |
| S1.2 | pydantic-settings 配置加载 + 前置检查 | S1.1 | 三层优先级生效；缺失必填项时按配置契约退出码 2 退出并打印可照做的修复提示；前置检查按配置契约 §2.1.1 **分级生效**实现（P1 只要求 P1 阶段变量）；测试用 settings 覆盖构造"缺失"与"齐全"两种情形，不读真实 `.env` | 1.5 | ✅ |
| S1.3 | ruff + mypy + pre-commit | S1.1 | ruff 全仓库零告警；mypy 严格模式**先对 `models/`、`config` 与新增代码生效**，存量模块逐阶段收紧（存量一次性全绿不现实）；提交前钩子生效 | 1 | ✅ |
| S1.4 | `tools/` 首批单测 | S1.1 | 词典识别、匹配打分、报告渲染、素材扫描各有用例；全部离线可跑（落地时扩到 tools 下 8 个可离线测的模块） | 2.5 | ✅ |
| S1.5 | 覆盖率统计接入 | S1.4 | `uv run pytest --cov` 可出报告；覆盖率不设门槛但需可查（落地为语句 + 分支统计，基线写进 `docs/技术栈.md`） | 0.5 | ✅ |

### E2 · 数据与检索

| 编号 | Story | 依赖 | 验收标准 | 人日 | 状态 |
| --- | --- | --- | --- | --- | --- |
| S2.0 | 评测集与示例素材包制作 | — | 按产品方案 §10.1 的标注标准完成 20 组用例（覆盖五类场景）；`evals/fixtures/demo_pack/` 含 12–20 条素材与旁车说明；**不依赖任何代码，可立即开工**（落地时七维度全标 + v1 冻结校验） | 3 | ✅ |
| S2.1 | Compose 起 Postgres + Redis | — | `docker compose up` 后数据库含 `vector` 与 `pg_trgm` 扩展，pgvector ≥ 0.5；镜像 tag 固定（实测 vector 0.8.6 / pg_trgm 1.6 / PG 16.15 / Redis 7.4.11） | 1 | ✅ |
| S2.9 | 集成测试基座 | S2.1 | 引入 testcontainers 与 pytest-asyncio，提供可复用的 Postgres / Redis 容器夹具；`uv run pytest` 默认只跑单元用例，集成用例以 `-m integration` 显式触发（实测：默认 289 单测 + 7 条集成用例、容器 10 秒级启动、无 Docker 时单条报错退出码 1）。**必须先于 S2.2 完成**：S2.2 起各 Story 的验收都要求集成用例通过，基座缺席会形成循环依赖 | 3 | ✅ |
| S2.10 | 退役 legacy JSON 索引与库级优化 | S2.4 | 删除 `tools/materials.py` 的 `build_index` / `load_index` / `save_index` 及对应单测（S2.4 起 DB 是唯一索引，JSON 路径与 uuid 身份双轨）；`sync_materials` 的大库场景改为只 select `path` + `fingerprint` 两列规划（实测：拆两个提交——`refactor` 删掉那套 JSON 索引函数（连带只服务它的 `index_path_for`/`IndexResult`，单测 493 → 479）、`perf` 把规划期改为只取两列并按需取回变更行；反向验证——临时改回整行加载后集成用例同样 9 条全绿，证明行为中性；同时补回删函数带走的两个覆盖缺口（无关键帧不调视觉、`max_vision_items` 预算封顶）） | 1.5 | ✅ |
| S2.2 | SQLAlchemy async 模型与会话 | S1.1, S2.1 | 五张表映射完整，字段与数据契约逐项对齐；testcontainers 集成用例通过（实测：逐列/逐约束/14 条索引/级联/向量维度全部核对通过，17 条集成用例） | 1.5 | ✅ |
| S2.3 | Alembic 初始迁移 | S2.2 | `upgrade head` 建齐表与索引；`downgrade` 可回滚且不删扩展；迁移往返（upgrade→downgrade→upgrade）集成用例通过（实测：`alembic check` 零差异、扩展兜底路径与 CLI 冒烟都过，6 条集成用例） | 1 | ✅ |
| S2.4 | 素材索引入库 + 增量判断 | S2.3 | 按 `fingerprint` 增量更新；重复运行不重复打标；**重新索引后磁盘上已消失的素材在库中无残留行**（数据契约 §一 的删除侧承诺，避免匹配榜推荐已删除的文件）；**触发路径明确：由分析任务的前置步骤自动做增量索引，首次全量建库走运维命令（不进 HTTP 契约）**；集成用例通过（实测：`plan_sync` 纯函数单测 + 8 条同步集成用例，含新增/更新/删除/未变四条路径与 CLI 冒烟） | 2 | ✅ |
| S2.5 | Embedding 客户端与向量回填 | S2.4 | 批量调用 + 重试；`embedding_model` 与向量同时写入；集成用例通过（实测：DashScope 兼容端点单请求批量硬上限 **10**——契约原默认 16 已修正为 10 并由客户端封顶；维度实测 1024；8 条集成用例覆盖写入/幂等/换模型重建/内容变更补行/失败即停/`<=>` 排序/CLI） | 1.5 | ✅ |
| S2.6 | 双通道召回 + RRF + 要素加权 | S2.5 | 排序、平局规则、截断全部符合检索契约；`recall_sources` 有值；**报告展示向量覆盖率**（本次参与检索的素材中有向量的比例，对应检索契约 §三 与 §十）；集成用例按检索契约第八节参数表逐项验证（实测：补上 `schemas.MatchCandidate.recall_sources` 的契约漂移；`hit_threshold` 此前被 `hits` 的固定 0.5 门槛挡死、已改为覆盖度按配置判定；20 条工具单测 + 15 条集成用例覆盖八个参数项） | 3 | ✅ |
| S2.7 | 清理离线降级链路 | S1.4 | ① 删除 `tools/offline.py`；② `llm.py` 移除 `OfflineProvider` 与 `build_provider` 的 offline 分支；③ `config.py` 取消 `auto` / `offline` 语义（缺密钥即失败）；④ `config.toml` 与 `.env.example` 中"密钥为空走离线"的注释同步清除；⑤ 全量测试绿（实测：③④ 早在 S1.2 就是既成事实、本次只做验证；① 连同 `hotspot_clue`/`copy_draft` 一并删除，规则版匹配解释内联进 `tools/matching.py` 且文案逐字未变；顺带删掉只被测试使用的 `score_material` 与 legacy `rank_materials`，仓库只剩 `match_elements` + `retrieval.rank_candidates` 一条打分链；新增 4 条"不许复活"守卫用例） | 0.5 | ✅ |
| S2.8 | 基线对比脚本与结果回填 | S2.6, S2.0 | 用 S2.0 的 20 组用例跑出「纯字面基线 vs 混合召回」对比数字；**本轮只测检索层可测的五项**（Top-5 命中率、首选命中率、线索覆盖度、单次耗时、单次成本），**文案可用率不在本轮**——它需要撰稿环节，留到 E3 的 S3.6 之后补测；回填产品方案 §10.2 时按"测量阶段"列标注。实测（ADR 0010 重标定后）：Top-5 100%、首选 88.9%、覆盖度 100%、耗时均值 1.96 s、成本 5.25e-06 元，五项全部达标 | 2 | ✅ |

> **E2 开工前置条件（DoR）· embedding 维度校准**
> S2.2 开工前，用真实密钥调用一次 embedding 接口，实测输出维度并记录。若 ≠ 数据契约 §3.1 的 `vector(1024)`，**先补 ADR、按检索契约 §九 修订数据契约与检索契约，再开工**。
> 为什么卡在这里而不是 S2.5：`vector(1024)` 在 S2.3 的初始迁移里就固化了，等 S2.5 才发现不一致，迁移、模型、回填、契约全部要返工。
> 判定标准：记录里写明模型名、调用参数与实际维度；一致则直接开工，不一致则 ADR 编号可查。
>
> **DoR 结论（2026-10-02）**：模型 `text-embedding-v3`、调用参数 `input=<14 字短文本>`、未传 `dimensions`（取服务端默认），**实测输出 1024 维**，与数据契约 §3.1 的 `vector(1024)` 一致 → 契约无需修改，S2.2 已按此开工。校准脚本：`scripts/check_embedding_dim.py`。

### E3 · 编排与服务

| 编号 | Story | 依赖 | 验收标准 | 人日 | 状态 |
| --- | --- | --- | --- | --- | --- |
| S3.0 | Prompt 契约与资产骨架 | S0.1 | `docs/contracts/prompt契约.md` 定义五段结构与变更门槛；`src/xhs_agent/prompts/` 四份文件五段齐全；`tools/prompt.py` 零依赖渲染（缺变量／缺段／requires 不符即报错）；`tools/vision.py` 改为从 prompt 文件加载；单测覆盖全部失败路径 | 2.5 | ✅ |
| S3.1 | LangGraph 状态图 | S2.6 | 拆解 → 检索 → 缺口 → 撰稿 → 报告五节点；节点级重试与进度上报可用；**三条 agent 任务的 prompt 正文按五段结构交付，并把各任务版本号写入 `runs.prompt_versions`**（实测：引入 langgraph 1.2，单热点图 + `run_analysis` 多热点循环；三条 prompt 正文按段拆 4 个提交交付、版本保持 v1；`material_select` 一次调用覆盖 Top-K 并覆盖规则解释；缺口节点产出结构化补拍建议；`prompt_versions` 在 state / `RunResult` / `state.json` 里产出、落库归 S3.4a；节点级重试需显式 `retry_on`——langgraph 默认不重试 `RuntimeError`/`ValueError`；顺带补齐 ADR 0010 漏掉的阈值默认值。新增 62 条单测 + 2 条集成冒烟） | 2.5 | ✅ |
| S3.2 | FastAPI 应用骨架 | S1.2 | 应用可启动并挂载 `/api` 前缀下的路由树；**错误响应与 `openapi.yaml` 的 `ErrorResponse` 逐字段一致**（集成用例断言 code / message / detail 三键）；未捕获异常返回 500 且不泄漏堆栈；响应回写 `X-Request-ID` 且与请求头一致（实测：引入 fastapi 0.142.2 + uvicorn 0.54.0（httpx 0.28.1 先落 dev 组）；`api/` 三个空 router 挂 `/api`，五个接口归 S3.3、不提前引 redis；错误码与 HTTP 状态严格按契约配对、非契约状态（405）归一化并留原状态于 detail；中间件兜住未捕获异常，**500 也带 X-Request-ID**；文档页与 openapi.json 一并挂 `/api` 下以配合 ADR 0009；36 条离线骨架用例（放 unit 而非 integration——不需要 Docker）） | 2 | ✅ |
| S3.3 | 五个接口实现（`/api` 前缀） | S3.1, S3.2, S2.3 | 逐个对齐 OpenAPI 契约；错误码与响应结构一致；接口级集成用例通过（实测：新增 `services/runs.py`（提交：hotspots 按原文去重复用 + runs(queued) + run_hotspots 骨架；读取：JobStatus / RunDetail / 报告 model）；`runs` 补 `topk` 与 `request_id` 两列（数据契约 §3.3/§4.2 + Alembic `0003`，含手工补的 CHECK——autogenerate 不比对 CHECK）；`redis` 提为运行时依赖，`/api/health` 真探活 db/redis/llm；**发现并修复契约硬伤**：409 的响应体是 `ErrorResponse` 但错误码枚举里没有对应 409 的码，新增 `conflict`(409)；analyze 的投递用可注入 dispatcher，默认显式 503（Celery 属 S3.4b）；未完成运行取结果/报告返回 409，非法 uuid 按 404；新增 18 条离线用例 + 13 条接口集成用例） | 2.5 | ✅ |
| S3.4a | 分析结果写回 | S3.1, S2.3 | **S3.3 已写 `hotspots`（按原文去重复用）+ `runs`(queued) + `run_hotspots` 骨架，本 Story 只做 worker 侧写回**：按数据契约 §3.5 写 `run_matches`、回填 `run_hotspots` 的 coverage/draft/status、把 `prompt_versions` 与 LLM 统计（llm_calls / tokens / cost / latency）与 `runs.status` / `finished_at` 落到 `runs`；`(run_hotspot_id, rank)` 幂等；`reasons` 非空约束在 Pydantic 与数据库两处都生效；单测 + 集成用例覆盖。**RunStore 与 DB 的权威关系按 ADR 0011 执行**（停写 state.json）（实测：新增 `services.runs.RunRecorder`（`start` / `load_clue` / `hotspot_finished` / `finish`）与 `planned_hotspots` / `match_row_values`；`run_analysis` 增加 `run_id` / `topk`，**每个热点跑完立即写回**——`hotspots.clue` 回填 + `run_hotspots` 状态/覆盖度/草稿 + `run_matches` 先删后插保证幂等，收尾写 `runs` 的状态 / `finished_at` / 统计 / `prompt_versions`；**线索复用**：`hotspots.clue` 非空即跳过拆解节点（数据契约 §3.2"避免重复付费"的真正兑现，该次运行不记 `hotspot_clue` 版本）；**收口 S3.3 的 `runs.topk` 死参数**——`retrieve_candidates` 新增 `topk` 覆盖，优先级 显式 `topk` > `runs.topk` > `cfg.match.topk`；`tools/trace.py` 按 ADR 0011 停写 `state.json`（`save_state()` → 纯内存 `pending_steps()`，`finalize()` 返回运行目录），产物目录改为 `runs/<run_id>/`，并把该模块纳入 mypy 严格范围（原 25 处报错清零）；`reasons` 非空在 Pydantic（`SchemaError`）与数据库 CHECK 两处都生效；新增 12 条单测 + 7 条集成用例，单测 510 / 集成 84 全绿） | 1.5 | ✅ |
| S3.4b | Celery worker 接线与状态回写 | S3.3, S3.4a, S3.5 | 任务状态与 `runs.status` 同步（含失败路径）；软/硬超时生效；**在进入 LangGraph 前完成索引新鲜度检查与增量索引**；集成用例通过（实测：新增 `src/xhs_agent/tasks/`（`celery_app.py` 纯工厂 / `analysis.py` 任务体与同步外壳 / `worker.py` 启动入口）；引入 celery 5.6.3；`api/deps.get_dispatcher` 换成 `CeleryDispatcher`（延迟导入 + `asyncio.to_thread` 发消息，broker 不可达折成 503 且 `submit_analysis` 整体回滚）；`probe.CURRENT_STAGE` P2 → **P3**（`REDIS_URL` 变必填）；`services/runs.mark_run_failed` 兜底失败路径。**索引新鲜度 = 增量同步 + 向量回填**，两步都在进 LangGraph 前跑完；**重复投递幂等**（`runs.status` 终态直接返回，一次模型调用都不再发）；**重试**：基础设施类错误按 `[queue].max_retries` 重试、分析类失败不重试、`SoftTimeLimitExceeded` 直接标 failed。**两处偏离计划并已记录**：① 不设 result backend（ADR 0011：DB 是唯一权威源；设了后端 broker 不可达时会抛裸 `RuntimeError` 且要等 6.8 秒，不设则直接抛 `kombu.OperationalError`，能精确映射 503）；② mypy 对 `celery.*`/`kombu.*` 放行缺类型（两者都没有 `py.typed`），本仓库代码仍走 strict。真实冒烟：`POST /api/analyze` → 202 → worker 日志出现"索引新鲜度：素材同步…/向量回填…" → `queued → running → succeeded`（progress 0 → 1），`llm_calls=1`、`completion_tokens=3083`、cost 0.0296 元；新增 16 条单测 + 6 条集成用例，单测 541 / 集成 92 全绿） | 2 | ✅ |
| S3.5 | httpx 替换 urllib | S2.5, S2.6 | 异步调用 + 连接池 + 重试策略；失败映射到 `upstream_error`；**调用方一并接线**——`services/retrieval.py` 与 `services/materials.py` 的 embed 调用改为 `await`（去掉临时的 `asyncio.to_thread` 包裹，只换客户端不改调用方等于没修）。**须在 S3.4b 之前完成**（E2 审查建议 1 的时序风险）。顺带把向量字面量拼接改为 asyncpg 原生参数绑定（E2 审查项 7）（实测：新增 `tools/http.py`——`build_http_client`（连接池 `max_connections=10` / `keepalive=5` / `expiry=30s`，调用方 `async with` 持有，不做进程级单例）+ 纯函数 `backoff_seconds`；`tools/llm.py` 与 `tools/embedding.py` 改异步 httpx、**客户端由调用方注入**（`build_provider` / `build_embedder` 的 `client` 是必填关键字参数），重试口径逐字保留（`max_retries+1` 次、退避 `min(8, 1.5^n)`、400/401/403 不重试），`Transport` 与 `_urlopen_json` 删除；`agents/*` 三个函数与 `StructuredCaller.call` 变 async，`run_analysis` 起**一个 run 级客户端**注入 provider 与 embedder（`retrieve_candidates` 新增可选 `http` 参数以复用连接池）；`services/retrieval.py` 与 `services/materials.py` 去掉两处 `asyncio.to_thread`，`vector_recall` 改 `bindparam("v", type_=Vector(1024))` 绑 Python list；`api/errors.py` 增 `LLMError`/`EmbeddingError` → `upstream_error`(502)；httpx 从 dev 组移入运行时；mypy 严格范围新增 `tools/llm.py`（35 个文件零错误）。**连池用可观测证据钉住**：集成用例用进程内 uvicorn 记录 `scope["client"]` 端口，16 条文本（两批 10+6）只开 **1 条 TCP 连接**，反证用例则证明两个客户端开两条。**`tools/vision.py` 与 `scripts/check_embedding_dim.py` 保持 urllib**（同步建索引路径，本 Story 显式裁掉）。新增 15 条单测 + 2 条集成用例，单测 525 / 集成 86 全绿） | 1 | ✅ |
| S3.6 | Walking Skeleton 联调 | S3.4b, S3.5 | 见第四节的最薄链路定义（实测：新增 `scripts/smoke_skeleton.py` 一键联调——① 复用 `probe.preflight` 校验配置；② 素材库为空时把冻结的示例素材包 `evals/fixtures/demo_pack` 种进 `[paths].materials_dir`（库非空绝不碰用户素材）；③ 自己起 uvicorn + Celery worker（空闲端口、日志落 `runs/_smoke/`、`--pool=solo` 兼容 Windows）；④ 等 `/api/health` 200 与 worker 的 control ping 应答（任务卡在 queued >30s 直接报"worker 没在消费"）；⑤ 用 case-01 的热点投递并轮询到终态；⑥ **断言 §四 的硬条件**——至少 1 条候选且首选带命中要素与理由、报告是完整 HTML 且含热点原文；⑦ 两份报告落 `evals/reports/skeleton-<日期>.{html,md}`；⑧ finally 回收进程树。真实跑通：候选 **5 条**、首选「球场热身挥拍」命中 **7 个要素**、得分 0.965，报告 HTML 9051 字符，3 次模型调用、0.0926 元、端到端 65.6s，跑完无残留进程；**联调顺带发现一处既有缺陷**（关键帧缩略图渲染不出来，见风险表）。新增 23 条单测（纯函数：种包/断言/命名/命令/参数），单测 580 / 集成 92 全绿） | 1 | ✅ |
| S3.7 | 前端静态资源挂载 | S3.3 | `frontend/dist` 挂载到根路径并支持 history 回落；`[frontend].serve = false` 时只跑 API；`dist` 缺失且 serve 为 true 时启动失败（退出码 2）。`/api` 前缀的验收归 S3.3，本 Story 不重复（实测：新增 `api/frontend.py`——`SPAStaticFiles`（`/api` 前缀一律交回 API 的 404；未命中的**无扩展名**路径回落 `index.html`；带扩展名的未命中保持 404）+ `mount_frontend`（`serve=true` 且 dist 存在且非空才挂，挂载点记 `app.state.frontend_mounted`，重复不挂两次）+ `require_dist_dir`（供 S3.8 第 7 步，修复提示含 `pnpm build`；`serve=false` 返回 None）；`AppConfig` 增 `frontend_dist_dir()`；`create_app(cfg=...)` 立即挂载、否则在 **lifespan** 里按 `app.state.config or get_config()` 挂（保住"导入期不读配置"）。**边界**：`dist` 缺失时的**启动中止**（退出码 2）归 S3.8——配置契约 §四把第 7 步列为 7 步 preflight 的一项，lifespan 接线与退出码是 S3.8 的范围；本 Story 交付该助手与单测。真实冒烟：临时 dist + uvicorn → `/` 200 HTML、`/runs/abc` 200 HTML（history 回落）、`/assets/app.js` 200 `text/javascript`、`/assets/missing.js` 404 JSON、`/api/nope` 404 `not_found` JSON、`/api/openapi.json` 200。新增 16 条单测，单测 557 / 集成 92 全绿） | 1 | ✅ |
| S3.8 | 完整 preflight | S3.7, S2.1 | 逐项实现配置契约 §四的 7 步检查：配置合法性、DB 连接、扩展版本、Alembic head、Redis PING、前端 dist、模型探测不入启动；退出码 2/3 与报错文案对齐契约；**检查接入 FastAPI lifespan（启动事件）**——任一步失败即中止启动（实测：`probe.py` 从"只查配置"扩到 7 步——第 3 步短生命周期 NullPool 引擎跑 `SELECT 1`（失败打印**脱敏 DSN**）、第 4 步查 `pg_extension` 且 pgvector ≥ 0.5.0、第 5 步比对 `alembic_version` 与 `ScriptDirectory` 的 head（表不存在 = 未迁移）、第 6 步 `redis.asyncio` PING、第 7 步复用 S3.7 的 `dist_dir_if_available`；`Problem` 加 `step` / `kind`，`exit_code` 由首个失败问题的 kind 推出 2/3，**按契约顺序执行、首个失败即停**。**退出码分工**：uvicorn 在 lifespan 失败时**固定退 3**（实测抛 RuntimeError 或 SystemExit(2) 都一样），所以新增薄启动包装 `python -m xhs_agent.serve`（先查 7 步、精确退 2/3，再 `create_app(cfg, check_startup=False)` 起 uvicorn）作为推荐入口，`uvicorn ...:app` 仍可用但失败恒退 3——这条边界写进 README 与 probe docstring。`create_app(cfg=None, *, check_startup=True)` 给测试与已自查的调用方开旁路；新增 `XHS_FRONTEND_SERVE` 环境变量覆盖（契约 §2.2 加一行），没前端产物时本地仍能按契约跑 API-only（README 与 `smoke_skeleton.py` 已默认用它）。子进程实测：缺必填项退 **2**、DB 指向死端口退 **3**；集成用例在真容器上验"干净库报未迁移（提示 `alembic upgrade head`）→ `upgrade head` 后 7 步全绿 → `DROP EXTENSION pg_trgm` 报缺扩展"。新增 25 条单测 + 3 条集成用例；单测 605 / 集成 95 全绿） | 1.5 | ✅ |
| S3.9 | Prompt 版本落库 | S2.3, S3.0 | 数据契约 §3.3 加 `prompt_versions`（jsonb，默认 `{}`）、API 契约 `RunDetail` 暴露同名字段、ORM 列、Alembic `0002` 可往返、集成用例覆盖列清单与默认值；**写入接线归 S3.4a** | 1.5 | ✅ |
| S3.10 | Prompt 变更回归报告 | S2.8, S3.0 | 复用评测脚本产出 `evals/reports/prompt-<task_id>-v<N>-<日期>.md` 对比报告模板与归档规则；落实 prompt 契约 §六 的七维度硬门槛判定；**报告附人工 1–5 分「文案可用率」列**（口径按产品方案 §10.2），只记录、不阻断回退判定——它没有产出物就无法打分，因此不进硬门槛（实测：新增 `scripts/eval_prompts.py`——`--task` 指定改的是哪个 prompt、`--baseline <git-ref>`（默认 HEAD）用 `git show` 取旧正文，**只在工具层 monkeypatch `prompt_tool.load`** 当基线臂（`render()` 无缓存，所以立即生效；不写文件、不改生产代码、不违反 prompt 契约 §七）；两臂在同一进程、同一临时库、同一份 demo_pack 上逐用例跑完整五节点链路（`run_analysis`，真模型），拿 `RunResult` 的 clue/coverage/candidates/draft/totals 打分。**七维度判定**：幻觉＝forbidden 命中 + must_not 召回数；事实准确率＝Σ must 命中 ÷ Σ must 总数（要素命中按"类型一致 + 取值 normalize_text 后相等或互相包含"判，标注写「健身」、产出「健身房撸铁」算命中），并另列 allowed 覆盖与"产出可回溯率"作观察项；输出结构＝逐用例重跑契约校验；Tool 选择只判本回合可观测子集（`recall_both_channels` **按通道级 union**、`no_candidate_padding`），其余期望标 N/A 并写明原因；任务完成率沿用 S2.8 口径（正确答案＝must_hit∪acceptable、全缺口用例不进分母）；成本/速度取均值进门槛。任一进门槛的维度不达标即 verdict=**回退**（脚本仍退 0——回退是给人看的结论不是脚本故障）。**两次真冒烟**（`--limit 2`，真容器 + 真模型，共约 0.8 元）：首次暴露两处**定义写错**并当场修正——① 事实准确率一开始按"产出要素精确率"算，而契约口径是 must 命中率（精确率会把合理的额外要素误判成不准确）；② `recall_both_channels` 一开始按"每条候选都要双通道"算，而评测集 §三 是**通道级**的（候选集合里两条通道都要出现）。修正后冒烟结果：幻觉 0 ✅、事实准确率 0.5→1.0 ✅、输出结构 1.0 ✅、Tool 1.0 ✅、任务完成率 Top-5 100% / 首选 50% ✅、速度 41.5→54.1 秒 ✅，**唯一红灯是成本**（0.094→0.120 元/热点，见风险表）。新增 34 条单测（按路径加载脚本，纯函数离线可跑），单测 635 / 集成 95 全绿） | 1.5 | ✅ |

### E4 · 可观测与交付

| 编号 | Story | 依赖 | 验收标准 | 人日 | 状态 |
| --- | --- | --- | --- | --- | --- |
| S4.1 | structlog + run_id 贯穿 | S3.2 | 每条日志带 run_id 与 request_id；JSON 输出可被检索 | 1 | ⬜ |
| S4.2 | Langfuse 接入 | S4.1 | 模型调用可看到 token、成本、延迟；`LANGFUSE_*` 不齐时只告警不阻断 | 1 | ⬜ |
| S4.3 | OpenTelemetry 追踪 | S4.1 | 一次请求可从 API 追到数据库与模型调用 | 1.5 | ⬜ |
| S4.4 | 多阶段 Dockerfile + 全栈 compose | S3.4b | `docker compose up` 一条命令起 api / worker / postgres / redis | 2 | ⬜ |
| S4.5 | GitHub Actions CI | S1.3, S1.4 | lint + typecheck + test 三关通过（推送后生效） | 1 | ⬜ |
| S4.6 | 契约一致性校验进 CI | S4.5, S3.3 | FastAPI 导出的 schema 与 `openapi.yaml` 不一致时 CI 失败 | 1 | ⬜ |

### E5 · 前端工程

| 编号 | Story | 依赖 | 验收标准 | 人日 | 状态 |
| --- | --- | --- | --- | --- | --- |
| S5.1 | Vite 工程脚手架 | S3.7 | Vue 3 + TS + Router + Pinia + Naive UI + Tailwind 可跑起来；目录结构符合 `docs/项目结构.md` | 2 | ⬜ |
| S5.2 | 契约生成接口类型 | S5.1 | `openapi-typescript` 生成 `schema.d.ts` 并入库；CI 校验生成结果无变化 | 1 | ⬜ |
| S5.3 | 分析台页面 | S5.2, S3.4b | 可输入多个热点、提交、轮询进度并跳转结果 | 2.5 | ⬜ |
| S5.4 | 结果详情页 | S5.3 | 要素标签、候选素材（含关键帧）、覆盖缺口、文案初稿四块齐全，文案可一键复制 | 3 | ⬜ |
| S5.5 | 数据可视化 | S5.4 | 覆盖度、得分分布、成本与耗时用 ECharts 呈现 | 2 | ⬜ |
| S5.6 | 运行历史 | S5.2 | 列出历史运行，可回看结果并下载报告 | 1.5 | ⬜ |
| S5.7 | 视觉打磨 | S5.5 | 三页共用同一套主题 token 与组件库；暗色主题下正文文本对比度 ≥ 4.5:1；375px 宽度无横向滚动；Lighthouse 性能分 ≥ 80 | 2 | ⬜ |
| S5.8 | 前端测试 | S5.4 | Vitest 覆盖工具函数与关键组件；`pnpm build` 产物可被服务端挂载 | 1.5 | ⬜ |
| S5.9 | 前端进 CI 与镜像 | S5.8, S4.4 | CI 增加前端 lint、测试与构建；多阶段镜像内含 dist | 1.5 | ⬜ |

## 四、Walking Skeleton（关键路径）

```
docker compose up
      ↓
POST /api/analyze  {"hotspots": ["某顶流明星打羽毛球"]}
      ↓  202 {job_id, run_id}
Celery 取任务 → LangGraph 五节点执行
      ↓
GET /api/jobs/{job_id} → succeeded
      ↓
GET /api/runs/{run_id} → 至少 1 条候选，带命中要素与理由
      ↓
GET /api/runs/{run_id}/report → 返回完整 HTML 报告
```

**必须在此之前完成的 Story**：S0.1、S1.1、S1.2、S2.1–S2.6、S2.9、S3.1、S3.2、S3.3、S3.4a、S3.4b、S3.5、S3.7。

前端（E5）**不在关键路径上**：Walking Skeleton 用 curl 或 OpenAPI 文档页即可验证，不必等前端完成。

这条链路跑通即视为 P1–P3 集成验证通过（前端 E5 与可观测 E4 不在其中）——它不追求功能完整（可以只有 1 条素材、1 个热点、1 条候选），但要求**每一层都被真实触碰过一遍**。

## 五、里程碑

| 里程碑 | 内容 | 判定 | 状态 |
| --- | --- | --- | --- |
| M1 契约冻结 | E0 完成 | 四份契约评审通过并入库 | ✅ 2026-09-30 |
| M2 质量基线 | E1 完成 | `uv run pytest` 与 `mypy` 全绿，`tools/` 行为不变 | ✅ 2026-10-01 |
| M3 检索可用 | E2 完成 | 评测集跑出混合召回相对纯字面基线的提升数字 | ⬜ |
| M4 端到端可演示 | E3 完成 | Walking Skeleton 跑通；OpenAPI 文档页可访问 | ⬜ |
| M5 可交付 | E4 完成 | `docker compose up` 起全栈；CI 三关全绿 | ⬜ |
| M6 前端可演示 | E5 完成 | 三个页面可交互跑通；前端类型与契约零偏差；dist 进入镜像 | ⬜ |

## 六、风险与未决项

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| Docker Desktop 未启动 / 镜像拉取受限 | S2.1 起无法推进 | **已缓解（2026-10-01）**：Docker Desktop 已运行（server 29.8.0），数据盘已在 `D:\docker\DockerDesktopWSL`，registry 加速器已配置；实测 `docker compose up -d --wait` 起服务成功 |
| 依赖安装需要网络，当前环境受限 | E1–E4 每阶段都被阻塞 | 提前统一授权安装类命令，避免每个 Story 卡一次 |
| embedding 模型实际输出维度与契约写的 1024 不一致 | S2.3 起全链路返工（维度在建表时固化） | 已升格为 E2 开工前置条件（见第三节的 DoR 说明），不再只是一句口头承诺 |
| Celery 同步模型与 async 代码的阻抗 | S3.4b 出现难排查的阻塞 | **已缓解（2026-10-03）**：统一定为"任务入口同步函数 + `asyncio.run`，engine 与 httpx 客户端都在任务内建、任务内关，禁止进程级连接池"，已写进 `docs/开发规范.md` §一 04；实现见 `src/xhs_agent/tasks/analysis.py` |
| `deepseek-flash` 是推理模型：输出预算被 `reasoning_tokens` 吃光 | 真实分析链路拿到空 content 或截断的 JSON | **已缓解（2026-10-03）**：`[llm].max_tokens` 2000 → **16000**、`timeout_s` 60 → **120**，并把"含推理 token 的总输出预算"这一口径写进《配置契约》§3.1。依据：同一 prompt 四次采样 `reasoning_tokens` = 1823 / 2215 / 4027 / 4061、正文约 1.6k token；`reasoning_effort` / `effort` 参数被端点**静默忽略**，所以只能抬预算（模型 `max_output_tokens` 393216，契约上限 32768） |
| 报告里的关键帧缩略图渲染不出来（显示"无预览"占位） | 演示效果打折：产品方案 §8.1 的"素材匹配榜附关键帧缩略图"落不了地（HTML 里 `<img>` 数为 0，尽管 `runs/_index/keyframes/` 里确实有 33 个关键帧） | **S3.6 联调发现（2026-10-03），未修**：两处缺口——① `services/runs.py` 的候选素材投影只带 id/path/type/title/description/tags，把契约 `MaterialSummary` 里**已声明**的 `keyframes` 丢了（实现与契约漂移）；② 即便带上，`tools/report.py` 用 `os.path.relpath(path, PROJECT_ROOT)` 生成 `runs/_index/keyframes/…` 的 `src`，而 API 不服务 `runs/`、样例报告也不在那个目录下。要修得先定"图片怎么引用"（内联 base64 / 新增取帧接口 / 渲染时复制到报告目录），属独立决策——建议排在 E5 出前端之前 |
| 端到端单热点成本约 0.09–0.12 元，**超过**产品方案 §10.2 的 `≤ 0.05 元/热点` 目标值 | S3.10 的 prompt 回归报告里「成本」这一维度会常年亮红灯（其余六项都过），它同时也是 `prompt契约` §六 的准入硬门槛之一——不解决就无法用这套回归流程判定"准入" | **S3.10 实测发现（2026-10-03），未修**：单个热点 3 次 `deepseek-flash` 调用，每次 2k–4k 推理 token（`max_tokens=16000` 是 S3.5/S3.8 为解决"空 content"抬上去的），按 `price_in_per_m=2.16` / `price_out_per_m=8.64` 折算约 0.10 元。三个方向需决策：① 调低 `[llm].max_tokens`（有回到"截断/空 content"的风险，除非同时换非推理模型）；② 换更便宜或非推理的模型（要同步配置契约与成本单价）；③ 修订 §10.2 的目标值把推理模型的实际单价写进去（属契约变更）。**建议与 E3 收尾一起决策**，别让它默默把 prompt 回归的门槛变成永远红 |
| 20 组评测用例的标注耗时被低估 | M3 延期 | 已拆出 S2.0 提前开工，标注标准先写进产品方案 §10.1 |
| 评测集由作者自建自评 | 数字说服力弱 | 标注标准先写进产品方案 §10.1，再按标准标注；必要时请他人复核 |
| 前端依赖数量多、审计成本高 | 供应链风险与构建不稳定 | 锁文件入库；直接依赖在提交信息说明理由；定期更新 |
| 前端类型与契约漂移 | 前后端联调返工 | 由 `openapi-typescript` 生成类型，并在 CI 中校验生成结果无变化 |
| 集成测试依赖 Docker，CI 上需可用 | E2／E3 的 DoD 无法达成 | GitHub Actions 自带 Docker；本地跑集成用例前先启动 Docker Desktop |
