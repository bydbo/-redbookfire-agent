# AGENTS.md · 小红书热点搭子（xhs-agent）

> 本文件是全仓库**唯一的 AI 开发规范**，权威版本。详细解释见 `docs/开发规范.md`，目录职责见 `docs/项目结构.md`，协作方式见 `docs/AI开发说明.md`——三者与本文件冲突时，一律以本文件为准。

---

## 0. 读取约定（先读这一节）

- 任何任务开始前，先完整读完本文件，再动手改任何东西。
- 涉及"某个文件该放哪"时查 `docs/项目结构.md`；涉及测试、依赖、提交时以本文件条文为准。
- 本仓库**只保留这一份 AGENTS.md**。需要给某个子目录加特殊规则时，改本文件对应小节，不要新建子目录 AGENTS.md——规则分散必然互相矛盾。
- 规范没覆盖到的情况：按"最小改动 + 不破坏既有契约"处理，并在同一次提交里把新规则补进本文件（**规范随代码一起演进**）。

---

## 1. 目录与代码位置

工程根 = 本文件所在目录。所有命令都在工程根下执行。

| 位置                         | 放什么                                           | 不放什么       |
| -------------------------- | --------------------------------------------- | ---------- |
| `src/xhs_agent/`           | 包源码                                           | 数据、密钥、临时文件 |
| `src/xhs_agent/tools/`     | 确定性能力：词典、模型调用、素材索引、匹配打分、报告渲染、运行追踪             | 流程编排       |
| `src/xhs_agent/agents/`    | 单点智能体（线索拆解、匹配解释、撰稿）                           | 跨步骤流程      |
| `src/xhs_agent/workflows/` | 把各步骤串成一次完整运行                                  | 底层算法实现     |
| `src/xhs_agent/db/`        | SQLAlchemy ORM 模型、会话工厂与引擎（表结构以数据契约为准）        | 业务流程、查询逻辑 |
| `src/xhs_agent/services/`  | 用例层：一次场景化操作（素材索引同步、分析、报告）；编排 tools/db/agents | 底层算法、HTTP 接口 |
| `src/xhs_agent/tasks/`     | Celery 任务层：worker 入口、任务体（重试 / 失败语义）与陈旧 run 对账 | 业务算法、HTTP 路由 |
| `src/xhs_agent/api/`       | FastAPI 路由、依赖注入、错误响应与 API 层模型（`models.py` 对齐 `docs/contracts/openapi.yaml`；统一 `/api` 前缀，ADR 0009）        | 业务逻辑、数据访问 |
| `src/xhs_agent/core/`      | 配置、日志、异常与常量（跨层共用）                                | 业务流程、HTTP 路由 |
| `src/xhs_agent/prompts/` | prompt 资产：一任务一份 Markdown，五段结构（契约见 `docs/contracts/prompt契约.md`） | 代码、逻辑实现 |
| `alembic/`                 | 数据库迁移脚本（唯一建表路径；`alembic.ini` 在工程根）              | 业务代码、手工改库 |
| `tests/`                   | 单元测试与集成测试                                     | 运行产物、真实素材  |
| `evals/`                   | 评测用例、标注数据、脱敏示例素材包 `evals/fixtures/demo_pack/` | 真实素材       |
| `config/`                  | `config.toml`（默认配置）、`.env.example`（密钥模板）      | 真实密钥       |
| `data/materials/`          | 本地素材库，**不入库**                                 | 任何需要提交的文件  |
| `runs/`                    | 运行产物，**不入库**                                  | 手写源码       |
| `scripts/`                 | 一次性运维与数据生成脚本（建库、生成示例素材包）                      | 业务流程、被运行时 import 的代码 |
| `docker/`                  | 容器初始化文件（PostgreSQL 扩展、数据库初始化；compose 与集成测试共用）；镜像与编排在根目录 `Dockerfile` / `.dockerignore` / `docker-compose.yml`（五服务）   | 应用代码、业务流程 |
| `docs/`                    | 全部文档：产品方案、简历与面试、技术栈、项目结构、AI 开发说明、开发规范、契约（`docs/contracts/`）、决策记录（`docs/adr/`）、Backlog（`docs/backlog.md`）             | 代码         |
| `frontend/`              | Vue 3 单页应用源码、构建与前端测试配置                     | 后端代码、密钥     |

五条硬约定：

1. `docs/contracts/` 是**数据与接口契约的事实源**；`src/xhs_agent/schemas.py`（P1 之后迁至 `models/`）是内存实现，必须与契约逐字段一致。新增字段或结构必须先改契约文件，再改实现，最后改调用方。
2. 工程根由 `src/xhs_agent/config.py` 上溯三层推导得出，因此 `src/`、`config/`、`data/`、`runs/` 必须保持同级。**不要单独移动其中任何一个**，否则配置与路径会集体失效。
3. 新增顶层目录前，先在 `docs/项目结构.md` 登记，再更新本表。
4. **显示名与代码标识分离**：项目显示名（小红书热点搭子）可以改，但 Python 包名 `xhs_agent`、分发名 `xhs-agent`、环境变量前缀 `XHS_` 保持不变；确需改名时按第 6 节流程单独处理，不要顺手替换。
5. **`src/xhs_agent/prompts/` 是 prompt 的唯一来源**：禁止在代码里内联 prompt 文本，也禁止引入模板引擎（确有需要时走第 6 节流程并补 ADR）；改 prompt 结构或渲染规则，先改 `docs/contracts/prompt契约.md`。

---

## 2. 文件边界

依赖方向只能单向：`workflows/` → `services/` → `{agents/, tools/, db/}` → `models/`（P1 之前为 `schemas.py`）。

- `tools/` 只放确定性能力，**不得反向调用 `agents/`、`services/`、`workflows/` 或 `db/`**。
- `services/` 是用例层（编排 tools/db/agents 的"一次动作"），**不重复实现 `tools/` 里已有的算法**，也不直接暴露 HTTP。
- `db/` 只依赖 `models/`（P1 之前为 `schemas.py`）与标准库/驱动（SQLAlchemy、asyncpg、pgvector），不依赖 `tools/`、`agents/`、`services/`。
- `agents/` 与 `workflows/` 只做编排与判断，不重复实现 `tools/` 里已有的算法。
- 数据契约的**内容**定义在 `docs/contracts/`（API / 数据 / 检索 / 配置四份），代码里的类型定义必须与之一致；`src/xhs_agent/schemas.py`（P1 之后为 `models/`）是内存实现，`docs/contracts/` 是事实源。**改契约必须先改契约文件，再改实现。**
- 契约未覆盖的能力不得先写代码——先补契约，再实现。
- 其他模块不得私自定义与契约并行的结构。
- 禁止在 `src/` 下写运行时数据；禁止在 `tests/` 下写运行产物；测试需要落盘时统一用临时目录并在用例结束时清理。
- 禁止把密钥、真实素材、运行产物写进任何会被提交的文件。
- 文档只放 `docs/`；仓库根的 Markdown 只允许 `README.md` 与 `AGENTS.md` 两份。
- 前端只放 `frontend/`，不得在 `src/` 里写前端代码；前端不得直接访问数据库，所有数据经 `/api` 获取。
- 接口类型必须由 `openapi-typescript` 从 `docs/contracts/openapi.yaml` 生成，禁止手写并行类型定义。

### 2.1 tool 层标准（`tools/` 下的每个模块都要满足）

1. **一个 tool 只做一件事**，用途能用一句话说清；多步流程归 `workflows/`。
2. **输入写清楚**：模块 docstring 用「用途 / 输入 / 输出」三段式写明参数类型、必需与可选、缺省行为。
3. **输出要稳定**：同一输入必须得到同一输出；**不得依赖系统时钟或全局状态**——需要"当前时间"时做成可注入参数（只有不传时才取系统时间），结构、字段名与排序都不能随机。
4. **不反向依赖** `agents/` 与 `workflows/`；工具之间也不靠互相兜底掩盖错误。
5. **每个 tool 都要能单独测试**：用例不联网、不读真实密钥、不依赖机器上装没装 ffmpeg（依赖探测必须可注入），落盘一律用临时目录。

### 2.2 评测集冻结（`evals/`）

- `evals/manifest.json` 的 `version` 就是评测集版本；**已冻结的版本不得原地修改**——改用例、换素材、删条目都算。
- 要调整就新增版本（`v2` 起）并保留旧版本，让历史评测数字可复现；每次结果必须记录所用版本号。
- 版本清单里每个文件的 sha256 由 `tests/unit/test_evals_fixtures.py` 校验：改了文件而没同步 manifest，测试直接变红。
---

## 3. 测试要求

- 框架：`pytest`；开发依赖（测试、覆盖率、静态检查类工具）见第 4 节的表。测试统一放 `tests/`，文件名 `test_*.py`。
- **所有测试必须能在没有 API 密钥、没有网络的机器上跑通**：外部调用一律用假实现或固定样本替代，禁止在测试里真调模型。
- 确定性优先：涉及素材扫描、匹配打分、报告渲染的测试，断言结构化结果（分数、命中要素、覆盖缺口），不要断言自然语言措辞。
- 涉及文件系统的测试必须使用临时目录，跑完不留残留。
- **集成测试独立成层**：放 `tests/integration/`，必须带 `integration` 标记；`uv run pytest` 默认只跑 `tests/unit/`（`addopts` 排除该标记），集成用例用 `uv run pytest -m integration` 显式触发。集成测试需要可用的 Docker（testcontainers 起真实 Postgres / Redis），**Docker 不可用时直接失败并说明原因，不静默跳过**——与项目「不做降级」的口径一致。
- 当前状态：`tests/unit/` 已有 `schemas.py` 契约测试，`uv run pytest` 应全绿；`tools/` 的行为回归测试见 `docs/backlog.md` 的 S1.4。

---

## 4. 依赖策略与技术栈

**三类依赖分开管**

| 类型 | 规则 | 当前状态 |
| --- | --- | --- |
| 运行时依赖 | 必须走第 6 节流程才能引入；引入后同步更新 `docs/技术栈.md` | pydantic 2.x + pydantic-settings 2.x + python-dotenv（S1.1 / S1.2）、sqlalchemy[asyncio] + asyncpg + pgvector + alembic（S2.9 / S2.2 / S2.3）、langgraph（S3.1）、fastapi + uvicorn（S3.2）、redis（S3.3，health 探活）、httpx（S3.5，模型与向量的异步客户端）、celery（S3.4b，任务队列）、structlog（S4.1，结构化日志渲染管线）、langfuse（S4.2，调用追踪门面 `core/tracing.py`）、opentelemetry-api + opentelemetry-instrumentation-fastapi（S4.3，DB span 与 ASGI 埋点；测试用 opentelemetry-sdk 在 dev 组）、pyyaml（S4.5，dev 组，只给 CI workflow 的静态校验用例用）、python-multipart（S6.6，FastAPI 处理 multipart 上传的硬依赖），见 `docs/技术栈.md` |
| 开发依赖 | 允许测试与代码质量工具 | `pytest`、`pytest-cov`、`pytest-asyncio`、`testcontainers[redis]`、`ruff`、`mypy`、`pre-commit`（版本见 `docs/技术栈.md`） |
| 系统级依赖 | 仅指外部程序依赖；缺失时只影响对应能力 | `ffmpeg` / `ffprobe` |
| 前端依赖 | 由 `frontend/package.json` 管理；直接依赖需在提交信息写明理由，禁止引入第二套组件库或状态管理方案 | pnpm 11.25.0 + Node 25.3.0 + echarts 6.1（S5.5，结果页三张图） |

- 依赖统一由 `pyproject.toml` 声明、`uv` 管理；`uv.lock` 提交入库。前端依赖由 `frontend/package.json` 声明、pnpm 管理，`pnpm-lock.yaml` 提交入库；`node_modules/` 与 `frontend/dist/` 不入库。
- Python 版本要求 `>= 3.11`（代码使用了 `tomllib`）。
- 完整技术栈清单以 `docs/技术栈.md` 为准，本文件只写规则。
- **引入企业级组件（Web 框架、数据校验、向量检索、数据库、容器化、可观测等）属于计划内演进**，按第 6 节流程走即可，不算违规。
- **不允许为了“简历好看”引入没有真实使用场景的组件**——每个依赖都要能回答“不加它会怎样”。
- **运行时不再提供降级路径**：缺密钥、连不上数据库或队列时**直接启动失败并明确报错**，不做静默降级。取消的是整条运行链路的降级；单个可选能力（如无 ffmpeg 时跳过抽帧）属于能力裁剪，不算降级。

---

## 5. 新增功能后怎么测试

按顺序做，缺一步就不算完成：

1. **先补测试再收尾**：新增功能必须带 `tests/` 下的测试用例；纯配置或纯文档改动可以免测，但要在提交信息里说明。
2. **跑质量门与全量测试**：在工程根依次执行 `uv run ruff check .`、`uv run mypy`、`uv run pytest`，三者的结果都必须是零告警/零错误；想看覆盖率用 `uv run pytest --cov`（不设门槛，基线见 `docs/技术栈.md`）。
3. **跑人工冒烟**：用最小输入走一遍真实链路，例如 `uv run python -c "import xhs_agent; print(xhs_agent.__version__)"` 与 `uv run python -m xhs_agent.probe`；涉及报告渲染的改动必须实际生成一份报告看一眼。
4. **守住契约与集成路径**：依赖外部服务的改动，必须补集成测试（用 testcontainers 起真实 Postgres / Redis）并在真实依赖下跑通。项目不做运行时降级——缺密钥、缺数据库时报错是预期行为，不要“修”成降级。
5. **更新文档**：行为或接口变了，同步更新 `README.md` 与 `docs/项目结构.md`；规则变了更新本文件。

---

## 6. 什么时候允许增加依赖

默认答案是"先论证，再引入"。同时满足以下条件才可以加：

1. 有真实使用场景，标准库确实做不到，或实现成本明显不合理地高；
2. 能写出一句"不加这个依赖会怎样"，放进提交信息；
3. 优先选择开发依赖；动运行时依赖时，必须同时更新 `docs/技术栈.md` 与 `README.md`；
4. 同步更新 `pyproject.toml`、`uv.lock`、`README.md`、本文件第 4 节与 `docs/技术栈.md`。

禁止事项：为了省几行代码加依赖；加一个只用到 5% 功能的库；**纯粹为了让简历上有某个关键词、项目里没有真实使用场景的组件**；引入会长期绑定某个云厂商 SDK 的依赖。

---

## 7. 提交前检查清单

每次提交前逐条自查：

- [ ] `git status` 里没有不该出现的文件：`config/.env`、`data/materials/` 下的素材、`runs/` 下的产物。
- [ ] 没有把密钥、Token、账号密码写进代码、文档或示例文件。
- [ ] `uv run ruff check .` 零告警、`uv run mypy` 零错误（mypy 当前只覆盖新代码，范围见 `pyproject.toml` 的 `[tool.mypy].files`）。
- [ ] `uv run pytest` 通过；提交前钩子已生效（`uv run pre-commit run --all-files` 全绿）。
- [ ] 新增或改动的功能有对应测试，或已在提交信息里说明为何免测。
- [ ] `pyproject.toml`、`uv.lock` 与实际 import 一致；依赖有变化时已同步 `docs/技术栈.md`。
- [ ] 行为变化已同步到 `README.md` / `docs/`；规则变化已同步到本文件。
- [ ] 提交信息符合 `feat / fix / docs / test / chore / refactor` 前缀，且**一次提交只做一件事**。

---

## 8. 密钥与源码分开保存

原则：**密钥只存在于本地环境，永远不进版本库。**

- 密钥模板是 `config/.env.example`（可以入库，只含变量名与占位符）。
- 真实密钥写在 `config/.env`，该文件被 `.gitignore` 强制忽略，**永不提交**。
- 代码读取顺序：`config/.env` → 环境变量覆盖。任何模块都不得硬编码密钥，也不得把密钥写进日志、报告或 `runs/` 产物。
- 支持的环境变量：`DEEPSEEK_API_KEY`（P1 起必填）、`DASHSCOPE_API_KEY`（辅助服务，供多模态打标与向量化，按能力启用时必填）、`DATABASE_URL`（P2 起必填）、`REDIS_URL`（P3 起必填）、`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`（三件套齐全才启用追踪，不齐只告警），以及 `XHS_LLM_PROVIDER`、`XHS_LLM_BASE_URL`、`XHS_LLM_MODEL`、`XHS_LLM_API_KEY`、`XHS_EMBEDDING_MODEL`、`XHS_LOG_LEVEL`、`XHS_CONFIG_PATH` 覆盖项。**必填项按阶段分级生效**，启动前置检查只校验当前阶段的必填项，完整口径见 `docs/contracts/配置契约.md`；就地自检命令见第 5 节。
- 配置与密钥分离：`config/config.toml` 只放可公开的默认值（模型名、路径、阈值），不放任何凭据。
- 怀疑密钥已泄漏时：立即停止提交，轮换密钥，再检查 `git log` 与 `git ls-files` 是否已入库。

---

## 9. 执行任务流程（规则复用，减少反复猜测）

收到任何任务，按这五步走，不要跳步：

1. **读规范**：读本文件第 0 节，确认本次改动落在哪些目录、受哪些边界约束。
2. **定位**：用搜索找到真实入口与相关文件，先读代码再改；不要凭文件名猜结构。
3. **改动**：最小化修改范围，保持既有数据契约与对外行为；顺带修无关代码属于越界。
4. **验证**：按第 5 节跑测试与冒烟，按第 7 节逐条自查。
5. **收尾**：更新文档与规范，然后提交（第 7 节的提交契约）。

判断困难时的优先级：**不破坏既有契约 > 最小改动 > 顺手优化**。

---

## 10. 任务开工与完成标准（DoR / DoD）

**DoR · 满足以下全部条件，任务才可以开工**

1. 契约已冻结：涉及接口、数据结构、检索参数或配置的任务，`docs/contracts/` 中已有对应定义；
2. 依赖已就绪：`docs/backlog.md` 中该任务的依赖项已标记完成；
3. 验收标准明确：写得清"看到什么算做完"，且不依赖主观判断；
4. 边界明确：知道要改哪些目录、明确不动哪些文件。

低于以上标准的任务，先把条件补齐再动手——这是防止返工最有效的一条。

**DoD · 满足以下全部条件，任务才算完成**

1. 测试通过：`uv run pytest` 全绿；新增功能带用例，涉及外部依赖的补集成用例；
2. 契约一致：实现与 `docs/contracts/` 无偏差；有偏差先改契约文件并在提交信息中说明原因；
3. 质量门通过：`uv run ruff check .` 零告警、`uv run mypy` 零错误（严格模式的覆盖范围见 `pyproject.toml` 的 `[tool.mypy].files`，存量模块逐阶段收紧）；
4. 文档同步：行为变化更新 `README.md` 与 `docs/项目结构.md`；规则变化更新本文件；
5. 成本可查：涉及模型调用的改动，在 `runs` 记录与报告里能看到 token 与成本；
6. 提交规范：一次提交只做一件事，信息前缀符合第 7 节约定。

---

## 当前项目状态（规范的诚实边界）

- 已完成：数据契约（S1.1）、配置加载与启动前置检查（S1.2）、质量门与覆盖率（S1.3–S1.5）、爆点词典、模型调用层、素材扫描与索引、要素级匹配与规则解释、报告渲染、运行追踪；P2 已全部完成（S2.0–S2.9：本地依赖编排、集成测试基座、ORM 与会话、Alembic 迁移、评测集 v1、素材索引入库、向量回填、双通道召回与 RRF、离线链路清理、检索层基线对比）；**E3 已全部完成（S3.0–S3.10）**：prompt 契约、LangGraph 五节点状态图与三条 agent prompt 正文、FastAPI 骨架与五个接口（S5.4 取帧、S5.6 列表后共七个）、分析结果写回、httpx 异步化、Celery worker 接线、前端静态资源挂载、Walking Skeleton 一键联调、完整启动前置检查（契约 §四 的 7 步并入 lifespan）与 prompt 变更回归报告；**E4 已全部完成（S4.1–S4.6）**：structlog 结构化日志（`log_format` = `console` / `json`，`request_id` 与 `run_id` 两键恒在）、run_id 全链路贯穿与陈旧 run 对账、Langfuse Cloud 调用追踪（一次运行一条 trace，每个热点一个 span、每次文本模型调用一个 generation，看得到 token / 成本 / 延迟），三件套不齐只 warning 不阻断、默认只上报元数据；OpenTelemetry 把「一次请求」串成一条 trace（FastAPI/ASGI 服务端 span → `xhs_agent.db` DB span → W3C traceparent 经 Celery 消息头 → worker 的运行与模型 span），DB span 只记操作与表名、不上报 SQL 语句；多阶段镜像 `xhs-agent:0.1.0` + compose 五服务（`docker compose up -d --wait` 一条命令起全栈，迁移由一次性 `migrate` 服务跑），lifespan 退出时 dispose 连接池并清配置缓存；GitHub Actions 四个 job（lint / typecheck / test / image）推 main 即生效，覆盖率门槛 78% 进 CI；`scripts/check_openapi.py` 按「契约 ⊆ 实现」校验 FastAPI 导出的 OpenAPI，跑在 CI 的 test job 与本地 pre-commit 里；**E5 的 S5.1 已完成**：前端工程脚手架落地（`frontend/`：Vue 3.5 + Vite 7 + TS 5.9 + Router + Pinia + Naive UI + Tailwind 3.4，`@` 别名、`VITE_API_BASE_URL` 环境变量、5173 端口 `/api` 代理、`pnpm build` 产 dist；Node 25.3.0 + pnpm 11.25.0 按配置契约 §五固定）；**S5.2 已完成**：`openapi-typescript` 7.13 从 `docs/contracts/openapi.yaml` 生成 `frontend/src/api/schema.d.ts`（504 行，入库），`pnpm run check:api`（重新生成 + `git diff --exit-code`）防漂移，反向自测验证过（契约注 description 漂移 → 非零；还原 → 归零）；CI 新增第 5 个 job `frontend`（Node 25.3.0 + pnpm 11.25.0 + `--frozen-lockfile` + `check:api`）；**S5.3 已完成**：分析台页面落地（`frontend/src/api/client.ts` fetch 封装 + `analysis.ts` 接口调用（类型全部派生自生成物）、`stores/analysis.ts` 提交—轮询状态机、`views/AnalysisView.vue` 多热点输入/进度轮询、`views/RunResultView.vue` 结果占位页；路由 `/` → 分析台、`/runs/:runId` → 结果占位；真实冒烟走通「2 热点 → 提交 → 排队中/分析中/已完成 → 自动跳转结果页」，run `8860c661-…` 成本 0.2202 元、耗时 102.9 秒）；**S5.4 已完成**：结果详情页四块落地（爆点要素 / 候选素材含关键帧 / 覆盖缺口 / 文案初稿一键复制），关键帧引用决策落地为新取帧接口 `GET /api/materials/{material_id}/keyframes/{index}`（契约同步、路径限制在关键帧缓存目录内、404 不区分原因），真实冒烟 2 热点四块齐全、4 张关键帧全部加载、剪贴板校验通过。**S5.5 已完成**：结果详情页新增「数据可视化」卡片，三张 ECharts 图（要素覆盖度含 60% 目标线 / 候选得分分布五档 / 成本与耗时含按热点预算线）全部由已获取的 `RunDetail` 计算，**不改契约、不新增接口**；`echarts` 6.1 直接依赖按需引入（`echarts/core` + `BarChart` + Grid/Tooltip/Legend/MarkLine/Aria + `CanvasRenderer`），薄封装 `components/EChart.vue`，option 构建抽成纯函数 `charts/options.ts`；真实冒烟 16 条断言全过（canvas 真渲染、tooltip 数值与 `runs` 行逐项一致、控制台零报错、375px 图表卡片不溢出），截图落 `runs/_smoke_s55/`。**S5.6 已完成**：契约先行新增 `GET /api/runs`（`RunSummary` / `RunList`，`limit` 1–100 + `offset` 分页，接口共七个）——服务层 `list_runs` 按 `created_at` 倒序、平局按 id 倒序，`total` 独立 count，页内批量补热点数与首条热点预览（超 60 字截断），只读既有三表、不加迁移；前端 `/runs` 运行历史页（状态 / 短 id / 预览 / 时间 / 热点数 / 成本 / 耗时 / 调用数 + 查看结果 + 报告 HTML·MD 下载），queued·running 不可回看、failed 可回看（数据契约 §3.3），分析台加入口；真实冒烟 18 条断言全过（真实 8 行与 API 逐项一致、跳详情页、分页 offset 接线），截图落 `runs/_smoke_s56/`。**S5.7 已完成**：三页统一到一套小红书风格主题——品牌红 `#ff2442` 取自官网与其线上 CSS（`#FF2442` 配白字仅 3.76:1，故拆出 `#d81e3c` 供载白字按钮达标，13 对组合逐对核验≥4.5）；`theme/tokens.ts` 是唯一来源 → CSS 变量 → Tailwind 语义色，Naive 明/暗 themeOverrides 同源生成，毛玻璃作为 ADR 0008 的唯一例外只对按钮/顶栏/卡片加属性（ADR 0013）；三页改用语义类、0 处 `dark:` 变体，App.vue 成为带导航与暗色开关的壳层，图表跟随主题换调色板；性能上做路由级懒加载 + vendor/echarts 分包，并给托管 dist 的 API 挂 `GZipMiddleware`——移动端 Lighthouse 由 58 提到 **97**（总传输 1,011 KiB → 166 KiB），375px × 明暗 × 三页无横向滚动，无障碍 `color-contrast` 满分（剩下扣分项是 Naive 组件内部标记）。**S5.8 已完成**：前端补上 Vitest 5（jsdom + @vue/test-utils）48 条单测，覆盖主题 token 对比度与 CSS↔TS 一致性、三张图的 option 构建、API 客户端错误归一化、分析台提交—轮询状态机、运行历史页与 App 壳层；测试代码走独立 `tsconfig.vitest.json`（应用代码保持浏览器口径），覆盖率只报告不设门槛；测试当场抓到并修掉了结果页导航不高亮的真 bug。**S5.9 已完成**：前端 lint（ESLint 9 flat，只做正确性规则）与单测、构建一起进 CI 的 `frontend` job；镜像多一个 node 构建阶段（Node 25 无可用 corepack，pnpm 显式安装）把 `frontend/dist` 装进 runtime，compose 回到默认 `serve=true`——容器直接托管单页应用，镜像 413MB（+1MB），静态资源带 gzip。E5 至此全部完成。
- 运行时降级已取消（ADR 0001）：`tools/offline.py` 与 `OfflineProvider` 已在 S2.7 删除，不存在"无密钥也能跑"的路径；缺密钥、缺依赖一律失败并报错。
- **E5 前端工程已全部完成（S5.1–S5.9）**：脚手架、接口类型生成、分析台、结果详情、数据可视化、运行历史、视觉打磨、前端测试、前端进 CI 与镜像。下一步是 E6（新功能迭代，当前两个主题：素材库页 S6.1–S6.5、图片热点解析 S6.6–S6.9，合计 19.5 人日）。**业务链路已端到端跑通**：`POST /api/analyze` 投 Celery → worker 消费 → 索引新鲜度（增量同步 + 向量回填）→ 五节点分析 → 逐热点写回数据库 → `GET /api/runs/{run_id}` 取结果。当前可直接运行 `docker compose up -d --wait`（**一条命令起 api / worker / postgres / redis**；首次会构建镜像，前端 dist 未进镜像所以 api 用 `XHS_FRONTEND_SERVE=false`）、`uv run celery -A xhs_agent.tasks.worker:app worker --loglevel=info`、`uv run python -m xhs_agent.serve`（推荐启动入口：先跑 7 步启动前置检查，失败按契约退 2/3）、`uv run python -m xhs_agent.probe`、`uv run python -m xhs_agent.tasks.reconcile`、`uv run python scripts/index_materials.py`、`uv run python scripts/backfill_embeddings.py`、`uv run python scripts/eval_retrieval.py`、`uv run python scripts/smoke_skeleton.py`、`uv run python scripts/eval_prompts.py`；前端在 `frontend/` 下用 `pnpm install / pnpm dev / pnpm build`（Node 25.3.0 + pnpm 11.25.0，详见 README 步骤 15）（`REDIS_URL` 自 P3 起必填；`XHS_LOG_FORMAT=json` 出可检索的结构化日志；`LANGFUSE_*` 三件套齐全即启用调用追踪与 OpenTelemetry 埋点，缺任一只告警）。
- 记录以上状态是为了让 AI 与合作者先看清事实，不要把"计划要实现的东西"当成"已经有的东西"。
