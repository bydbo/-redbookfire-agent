# 小红书热点搭子

[![CI](https://github.com/bydbo/-redbookfire-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/bydbo/-redbookfire-agent/actions/workflows/ci.yml)

> Hotspot-to-Material Relevance Agent
> 输入一个热点，在**你自己的素材库**里找出能蹭上它的镜头，并说清为什么能蹭。

---

## 这个项目解决什么问题

热点的窗口期只有 24–48 小时，而创作者花在"翻素材"上的时间常常比拍素材还长：素材库几百上千条、命名靠手写、检索只能靠记忆。

热点搭子做三件事：

1. 把热点拆成**可迁移的爆点要素**（主题、场景、画面、情绪、形式、人群……）与**不可直用的要素**（明星姓名、肖像、代言暗示）；
2. 在你自己的素材库里做**确定性相关性检索**，输出候选镜头、命中的要素、建议怎么用，以及"缺什么素材"；
3. 给出一版**文案初稿**（标题备选、封面字、开头 3 秒、正文、话题、剪辑顺序）。

设计主线是：**文案是副产品，素材相关性才是真问题。** 大模型只负责判断（拆解、解释、撰稿），排序交给确定性算法——所以结果可复现、可解释、可评测，成本也不随素材量增长。

完整产品设计见 `docs/产品方案.md`。

---

## 当前进度（如实标注）

| 部分 | 状态 |
| --- | --- |
| 数据契约（要素／线索／素材／匹配／文案） | ✅ 已完成（S1.1 起为 Pydantic v2 模型） |
| 配置加载与启动前置检查（pydantic-settings，分级必填 + 退出码 2） | ✅ 已完成（S1.2） |
| `tools/` 首批单测（词典 / 打分 / 报告 / 素材 / 媒体 / 模型 / 追踪 / 视觉） | ✅ 已完成（S1.4） |
| 覆盖率统计（pytest-cov，语句 + 分支，不设门槛） | ✅ 已完成（S1.5） |
| 爆点要素词典（`tools/lexicon.py`） | ✅ 已完成 |
| 模型调用层（兼容接口 + 结构化输出自修重试） | ✅ 已完成 |
| 素材扫描与索引（旁车文件／文件名／视觉打标，增量更新） | ✅ 已完成 |
| 相关性打分与覆盖度计算 | ✅ 已完成 |
| 报告渲染（HTML + Markdown）与运行追踪 | ✅ 已完成 |
| `agents/` 与 `workflows/` 编排层 | ✅ 已完成（S3.1） |
| 本地依赖编排（PostgreSQL 16 + pgvector、Redis，Docker Compose） | ✅ 已完成（S2.1） |
| 集成测试基座（testcontainers + pytest-asyncio） | ✅ 已完成（S2.9） |
| 数据库 ORM 与会话（五张表、14 条索引、向量列） | ✅ 已完成（S2.2） |
| 素材索引入库 + 增量判断（`materials` 表 + 指纹增量 + 消失即删） | ✅ 已完成（S2.4） |
| 向量回填（embedding 客户端 + `materials.embedding` / `embedding_model`） | ✅ 已完成（S2.5） |
| 双通道召回 + RRF + 要素加权（素材检索服务） | ✅ 已完成（S2.6） |
| 检索层基线对比评测（纯字面基线 vs 混合召回，报告落 `evals/reports/`） | ✅ 已完成（S2.8） |
| LangGraph 五节点状态图（拆解 → 检索 → 缺口 → 撰稿 → 报告）与三条 agent prompt 正文 | ✅ 已完成（S3.1） |
| FastAPI 应用骨架（`/api` 路由树、统一错误响应、`X-Request-ID`） | ✅ 已完成（S3.2） |
| 五个接口实现（`/api` 前缀：health / analyze / jobs / runs / report） | ✅ 已完成（S3.3） |
| 分析结果写回（`run_matches` / 统计 / `prompt_versions` 落库，RunStore 退化为产物目录） | ✅ 已完成（S3.4a） |
| 分析主链路 HTTP 异步化（文本模型与向量共用 httpx 连接池，去掉 `asyncio.to_thread`） | ✅ 已完成（S3.5） |
| Celery worker 接线（`POST /api/analyze` 真投递 → 消费 → 索引新鲜度 → 分析 → 写回） | ✅ 已完成（S3.4b） |
| Walking Skeleton 端到端联调（一键脚本：起进程 → 投热点 → 验候选与报告 → 落样例报告） | ✅ 已完成（S3.6） |
| 完整启动前置检查（配置契约 §四 的 7 步 + 接入 lifespan + `python -m xhs_agent.serve`） | ✅ 已完成（S3.8） |
| Prompt 变更回归报告（两臂对照 + prompt 契约 §六 七维度门槛，落 `evals/reports/prompt-*.md`） | ✅ 已完成（S3.10） |
| 结构化日志（structlog，`console` / `json` 一键切换）与 run_id 全链路贯穿 | ✅ 已完成（S4.1） |
| 陈旧 run 对账（worker 启动时把卡住的 `running` 标成 `failed`） | ✅ 已完成（S4.1） |
| Langfuse 调用追踪（运行 / 热点 / 模型调用三级，看得到 token·成本·延迟；缺键只告警不阻断） | ✅ 已完成（S4.2） |
| OpenTelemetry 追踪（API 请求 → 数据库 → worker 模型调用连成一条 trace） | ✅ 已完成（S4.3） |
| 多阶段 Docker 镜像 + 全栈 compose（`docker compose up` 一条命令起 api / worker / postgres / redis，含一次性迁移服务） | ✅ 已完成（S4.4） |
| GitHub Actions CI（lint / typecheck / test + 镜像构建；覆盖率门槛 78%） | ✅ 已完成（S4.5） |
| 契约一致性校验（`scripts/check_openapi.py`：FastAPI 导出的 OpenAPI 必须覆盖 `docs/contracts/openapi.yaml`，跑在 CI 的 test job 里） | ✅ 已完成（S4.6） |
| Alembic 数据库迁移（唯一建表路径） | ✅ 已完成（S2.3） |
| 评测集 v1 与示例素材包（七维度口径、版本冻结） | ✅ 已完成（S2.0） |
| 服务入口（FastAPI 五个接口 + LangGraph 编排，S3.1–S3.3） | ✅ 已完成 |
| 前端工程脚手架（Vue 3 + Vite + TS + Router + Pinia + Naive UI + Tailwind，S5.1：别名 / 环境变量 / 代理 / 构建配置） | ✅ 已完成（S5.1） |

业务链路**已端到端跑通**：`docker compose up -d --wait` 起依赖 → `uv run celery -A xhs_agent.tasks.worker:app worker` 起 worker → `uv run python -m xhs_agent.serve` 起接口（先跑 7 步启动前置检查）→ `POST /api/analyze` 投递 → worker 自动消费（索引新鲜度 → 五节点分析 → 逐热点写回）→ `/api/jobs/{job_id}` 轮询 → `/api/runs/{run_id}` 取结构化结果、`/report` 取报告。另可运行 `uv run python -m xhs_agent.probe`、`uv run python scripts/index_materials.py`、`uv run python scripts/backfill_embeddings.py`、`uv run python scripts/eval_retrieval.py`、`uv run python scripts/smoke_skeleton.py`。路线图见文末。

---

## 技术栈

已落地的选型（完整清单、依据与规划中的演进见 `docs/技术栈.md`）：

| 层面 | 选型 |
| --- | --- |
| 语言 | Python ≥ 3.11 |
| 包管理与构建 | uv + `pyproject.toml`（hatchling 后端，src 布局） |
| 运行时依赖 | pydantic v2、pydantic-settings、python-dotenv、SQLAlchemy 2.0 async + asyncpg、pgvector、alembic、langgraph、fastapi + uvicorn、redis、httpx、celery、structlog、langfuse、opentelemetry-api + instrumentation-fastapi |
| 开发依赖 | pytest、pytest-cov、pytest-asyncio、testcontainers、ruff、mypy、pre-commit |
| 文本模型接入 | OpenAI 兼容 `/chat/completions` 协议（默认 DeepSeek），标准库 `urllib` 直连，JSON mode 结构化输出 + 解析失败自修 |
| 多模态接入 | 通义千问 VL（`qwen-vl-max`），关键帧 base64 内联 |
| 音视频处理 | ffmpeg / ffprobe（用于探测与抽帧；缺失时跳过抽帧，属能力裁剪） |
| 检索算法 | 双通道召回（pg_trgm 字面 + pgvector 余弦）→ RRF 融合 → 要素类型加权终分 → Top-K 截断；同分按 material_id 升序 |
| 配置 | pydantic-settings + TOML：环境变量 > `.env` > config.toml > 代码默认值；启动前置检查见 `uv run python -m xhs_agent.probe` |
| 数据落地 | PostgreSQL `materials` 表（素材索引，增量同步）+ JSONL 轨迹 + Markdown 与单文件 HTML 报告；JSON 索引已在 S2.10 退役 |
| 测试 | pytest |

### 最终技术栈（企业级，已确定待落地）

| 层面 | 选型 |
| --- | --- |
| Web 服务 | FastAPI + Uvicorn |
| HTTP 客户端 | httpx（异步 + 连接池；文本模型与向量的统一传输，S3.5） |
| Agent 编排 | LangGraph（状态图 + 检查点 + 失败重试） |
| 数据库与缓存 | PostgreSQL 16 + pgvector 0.8.6、Redis 7.4.11（本地 `docker compose` 起，tag 固定） |
| 数据访问 | SQLAlchemy 2.0 async ORM + asyncpg + pgvector（已落地，模型见 `src/xhs_agent/db/`）；Alembic 迁移待落地（S2.3） |
| 检索 | 混合召回：pg_trgm 字面 + pgvector 语义，RRF 融合后套要素加权 |
| Embedding | 默认通义 text-embedding-v3（API）；本地小模型为可选实现 |
| 队列与缓存 | Redis + Celery |
| 可观测 | structlog（`console` / `json`）+ run_id 贯穿全链路 + Langfuse Cloud 调用追踪 + OpenTelemetry（ASGI 服务端 span、`xhs_agent.db` DB span、W3C 跨进程传播；默认只上报元数据，`LANGFUSE_CAPTURE_CONTENT=true` 才连提示词与正文） |
| 容器化 / CI | Docker Compose（api / worker / postgres / redis 五服务）；GitHub Actions 四个 job（lint / typecheck / test / image） |
| 代码质量 / 测试 | ruff + mypy + pre-commit + pytest-cov + pytest-asyncio + testcontainers（均已落地） |
| 前端 | Vue 3 + Vite + TypeScript + Pinia + Vue Router（S5.1 已落地） |
| UI 与可视化 | Naive UI + Tailwind CSS（S5.1 已落地）+ ECharts（S5.5） |
| 接口类型 | `openapi-typescript` 从契约生成 TS 类型（S5.2） |

以上组件**尚未落地**，落地顺序见 `docs/技术栈.md` 第四节。**简历只写已经落地的技术栈。**

> **运行时降级已取消**：缺密钥、连不上数据库或队列时启动直接失败并明确报错，不再提供“无密钥也能跑”的离线模式。

## 快速开始

前置条件：Python ≥ 3.11、`uv`、`ffmpeg` / `ffprobe`（用于抽帧）。前端开发另需 Node 25.3.0 + pnpm 11.25.0（见步骤 15）。

> **注意**：两种跑法都支持——（A）**一条命令起全栈**：`docker compose up -d --wait`（下面第一段）；（B）本地开发：`docker compose up -d --wait postgres redis` 只起依赖，API 与 worker 用 `uv run` 直接跑（第二段起）。

```powershell
# ============ CI 跑什么（S4.5）：推 main 自动跑，本地可逐条复现 ============
#   lint        uv run ruff check .
#   typecheck   uv run mypy
#   test        uv run pytest -q --cov --cov-report=term-missing   # 覆盖率门槛 78%
#               uv run pytest -m integration -q                    # 真容器（runner 自带 Docker）
#   image       docker compose config --quiet && docker build -t xhs-agent:ci .
# test job 里还包含契约一致性校验（tests/unit/test_openapi_contract.py →
#   scripts/check_openapi.py），本地可单独跑：uv run python scripts/check_openapi.py
# Python 3.12 / uv 0.12.7 / UV_FROZEN=1；不依赖任何密钥。

# ============ （A）容器一条命令起全栈（S4.4）============
# 前置：Docker Desktop 已启动、config/.env 已填好密钥（DEEPSEEK_API_KEY / DASHSCOPE_API_KEY）
docker compose up -d --wait      # 首次构建镜像约 2–5 分钟；之后秒起
docker compose ps                # 期望：api / worker / postgres / redis healthy，migrate 为 exited(0)

# 健康检查（db / redis / llm 三项都 ok 才是 200）
curl -i http://127.0.0.1:8000/api/health

# 投一个热点并轮询（完整链路：队列 → worker → 索引新鲜度 → 五节点分析 → 落库）
curl -s -X POST http://127.0.0.1:8000/api/analyze -H "Content-Type: application/json" `
  -d '{"hotspots": ["某明星打羽毛球被拍"], "topk": 5}'
# 用返回的 job_id 轮询 /api/jobs/{job_id}；succeeded 后用 run_id 取 /api/runs/{run_id}

ls runs/                # 报告 / trace.jsonl 直接落在宿主（bind mount），容器里外同一份
docker compose logs -f api      # JSON 结构化日志（XHS_LOG_FORMAT=json）
docker compose down             # 停全栈（保留 pgdata / redisdata 卷）
# 迁移由一次性 migrate 服务自动跑；需要手动补跑时：docker compose run --rm migrate

# 容器口径：DATABASE_URL / REDIS_URL 指向 compose 服务名；api 以 XHS_FRONTEND_SERVE=false 启动
# （dist 进镜像归 S5.9，在那之前容器里只跑 API；本地已可用 frontend/pnpm build 产出 dist），
# 镜像以非 root（uid 1000）运行，
# ./data/materials 与 ./runs 是 bind mount——Linux 宿主上若 uid 不同，需要先 chown。

# ============ （B）本地开发：只起依赖，代码用 uv 跑 ============
# 1. 安装环境（首次需要网络）
uv sync

# 2. 起本地依赖（PostgreSQL 16 + pgvector、Redis；镜像 tag 固定在 docker-compose.yml）
docker compose up -d --wait postgres redis

# 3. 准备配置（必须填必填项：项目不做无密钥降级）
Copy-Item config/.env.example config/.env
#    编辑 config/.env 填 DEEPSEEK_API_KEY（P1 必填）、DASHSCOPE_API_KEY（向量召回默认
#    启用，故默认必填）、DATABASE_URL（P2 必填，本地默认
#    postgresql+asyncpg://xhs:xhs@localhost:5432/xhs）与 REDIS_URL（P3 必填，S3.4b 起
#    需要队列；**建议写 127.0.0.1 而不是 localhost**——compose 只绑 IPv4，Windows 上
#    localhost 常先解析到 ::1，会让 Redis 连接超时）
REDIS_URL=redis://127.0.0.1:6379/0

# 4. 建表（Alembic 迁移；改模型后必须同时提交迁移脚本）
uv run alembic upgrade head
#    回滚与一致性自查：uv run alembic downgrade base / uv run alembic check

# 5. 素材索引入库（可选，首次使用素材库时跑一次全量；之后每次分析前自动增量）
#    把素材放进 data/materials/ 后执行；--max-vision-items 控制视觉打标的成本上限
uv run python scripts/index_materials.py

# 6. 向量回填（首次建库后 / 素材内容变更后 / 更换 embedding 模型后各跑一次）
#    逐批提交、失败即停、重跑幂等；--batch-size 控制每批条数、--limit 限制本次条数
uv run python scripts/backfill_embeddings.py

# 7. 验证安装与本地依赖（两个服务应是 healthy）
uv run python -c "import xhs_agent; print(xhs_agent.__version__)"
docker compose ps

# 8. 验证配置读取（输出不应包含任何密钥）
uv run python -c "from xhs_agent.config import load_config; print(load_config().describe())"

# 9. 跑启动前置检查（《配置契约》§四 的 7 步：配置 → DB → 扩展 → 迁移 → Redis → 前端 dist）
#    退出码：0 = 通过；2 = 配置错误；3 = 依赖不可用（每一步都打印能照做的修复提示）
uv run python -m xhs_agent.probe

# 10. （可选）跑检索层基线对比评测：临时库只装 demo_pack，真实回填向量后两臂对照
#     结果落 evals/reports/retrieval-v1-<日期>.md；需要 Docker 与真实 DASHSCOPE_API_KEY
uv run python scripts/eval_retrieval.py

# 11. 起 Celery worker（S3.4b：消费 POST /api/analyze 投递的 job）
#     （走容器全栈时跳过这一步——compose 的 worker 服务已经在跑）
uv run celery -A xhs_agent.tasks.worker:app worker --loglevel=info
#    Windows 本地开发建议加 --pool=solo（prefork 在 Windows 上不稳；软超时只在
#    Linux 的 prefork worker 上生效）

# 12. 起 HTTP 服务（另开一个终端）
#    推荐用带前置检查的入口（先查 7 步、失败按契约退 2/3，再交给 uvicorn）：
uv run python -m xhs_agent.serve --host 127.0.0.1 --port 8000
#    等价写法：uv run uvicorn xhs_agent.api.main:app --port 8000（lifespan 也会查，
#    但那条路径启动失败时 uvicorn 固定退 3）
#    文档页：http://127.0.0.1:8000/api/docs（挂在 /api 下，非 /api 路径留给前端单页应用）

# 13. 投一个热点并轮询（完整链路：队列 → worker → 索引新鲜度 → 五节点分析 → 落库）
curl -s -X POST http://127.0.0.1:8000/api/analyze -H "Content-Type: application/json" `
  -d '{"hotspots": ["某明星打羽毛球场被拍，反差感拉满"], "topk": 5}'
#    拿返回的 job_id 轮询 /api/jobs/{job_id}，succeeded 后用 run_id 取 /api/runs/{run_id}
#    与 /api/runs/{run_id}/report?format=html

# 13.1 结构化日志（S4.1）：默认 console 可读；容器 / CI 与日志检索用 json
#      json 时一行一个 JSON 对象，每条都带 request_id 与 run_id（不在上下文里为空串）：
XHS_LOG_FORMAT=json uv run python -m xhs_agent.serve --port 8000
#      worker 同理：XHS_LOG_FORMAT=json uv run celery -A xhs_agent.tasks.worker:app worker
#      检索示例（只挑某次运行的日志）：… | jq -c 'select(.run_id=="<run_id>")'

# 13.2 陈旧 run 对账（S4.1）：worker 启动时会自动跑一次；也可手动补跑
#      把 running 且 started_at 超过 [queue].task_time_limit_s × 2（默认 1800 秒）的行标成 failed
uv run python -m xhs_agent.tasks.reconcile

# 13.3 调用追踪（S4.2）：把 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
#      三行写进 config/.env 即启用（缺任一：只打一条 warning 并关闭追踪，分析照常跑）；
#      LANGFUSE_HOST 也接受官方 SDK 的别名 LANGFUSE_BASE_URL；EU 区用 cloud.langfuse.com、
#      US 区用 us.cloud.langfuse.com。每次运行结束日志里会有一条带 trace 链接的
#      “调用追踪：run … 的 trace …”，按 run_id 就能回放。
#      默认只上报元数据（模型名 / task / run_id / token / 成本 / 延迟 / 错误），
#      需要连提示词与产出正文一起上报时再加 LANGFUSE_CAPTURE_CONTENT=true（ADR 0005 口径）。

# 13.4 一条请求一条 trace（S4.3）：POST /api/analyze 的服务端 span 会经 Celery 消息头
#      （W3C traceparent）接到 worker 的分析链路上，所以 Langfuse 里同一条 trace 里能依次看到
#      POST /api/analyze → db.submit_analysis → analyze → hotspot-1 → 3 个模型 generation。
#      DB span 只有操作与表名（不上报 SQL 语句）；/api/health 与文档页不产生 span。

# 14. 一键跑通 Walking Skeleton（推荐先用它验证环境）
#     自己起 uvicorn + Celery worker、素材库为空时种入示例素材包、投 case-01 的热点、
#     轮询到终态、核对"至少 1 条候选且带命中要素与理由"与完整 HTML 报告，跑完回收进程。
#     **真实调用模型**（一个热点约 3 次调用，成本几分钱）；样例报告落
#     evals/reports/skeleton-<日期>.html|md，这次运行会留在库里可回看。
uv run python scripts/smoke_skeleton.py
#     只驱动已起的服务：--api-url http://127.0.0.1:8000；不种素材：--no-seed

# 15. 前端开发与构建（S5.1 起；工程在 frontend/，Node 25.3.0 + pnpm 11.25.0，
#     版本固定在 package.json 的 engines / packageManager 与 .node-version）
cd frontend
pnpm install          # 首次；pnpm-lock.yaml 入库，node_modules 不入库
pnpm dev              # 开发服务器 http://localhost:5173，/api 代理到 127.0.0.1:8000（配置契约 §五）
pnpm run type-check   # vue-tsc 零错误才算过
pnpm build            # 类型检查 + 构建，产物落 frontend/dist（由 FastAPI 挂载，ADR 0009）
```

`pnpm dev` 与 `uv run python -m xhs_agent.serve --port 8000`（或 `XHS_FRONTEND_SERVE=false` 的等价入口）同时运行，即可在 http://localhost:5173 全栈调试：页面走 Vite，接口经代理进本地 API。

> **`[frontend]` 段怎么起作用**：`serve = true`（默认）且 `frontend/dist` 存在且非空时，FastAPI 会把 dist 挂在根路径，并把 `/api` 之外的未命中路径（无扩展名的）回落成 `index.html`——前端 history 路由刷新不会 404；带扩展名的未命中仍返回 404，API 的 404 也照旧是 `not_found` 的 JSON。
>
> **还没有前端产物时**：`serve = true` + 缺 dist 属于配置错误，启动前置检查会**拒绝启动**（退出码 2）。本地纯后端开发用环境变量覆盖即可只跑 API：`XHS_FRONTEND_SERVE=false`（等价于把 `[frontend].serve` 设为 `false`）——`scripts/smoke_skeleton.py` 已经默认这么做了。前端工程已落地（S5.1），本地 `cd frontend && pnpm build` 即可产出 dist。

网络受限时：`uv sync --no-dev` 只装运行时环境（pydantic / pydantic-settings / python-dotenv / SQLAlchemy async + asyncpg / pgvector / alembic）；不过 `import xhs_agent` 仍然需要安装或设置 `PYTHONPATH=src`（src 布局）。

### prompt 变更怎么走（prompt 契约 §六）

1. **一次只改一层**：单个提交只改五段中的一段（`src/xhs_agent/prompts/<task>.md`），版本号随内容递增；
2. **跑回归**：把被改那个 prompt 的新旧两版各跑一遍同一批 20 组用例——旧正文用 `git show` 取，
   不写文件、不改工作区：

   ```powershell
   uv run python scripts/eval_prompts.py --task hotspot_clue --note "本次改动的原因"
   # 冒烟（真容器 + 真模型，只跑前 2 组用例，约 0.4 元）：
   uv run python scripts/eval_prompts.py --task hotspot_clue --limit 2 --out <临时目录>
   ```

3. **看门槛**：脚本按契约 §六 判七维度（幻觉=0、事实准确率≥90% 且不降、输出结构 100%、
   Tool 选择 100%、任务完成率 Top-5≥80%/首选≥50% 且不降、成本≤0.15 元/热点（ADR 0012）、速度≤60 秒/热点），
   **任一不达标即输出"回退"**（脚本本身仍退 0——回退是给人看的结论）；
4. **归档**：报告落 `evals/reports/prompt-<task_id>-v<N>-<日期>.md` + 同名 `.json`，
   里面逐用例列维度变化、附改动 diff，并留一列人工 1–5 分「文案可用率」（**只记录、不进门槛**）。

> 代价参考：全量两臂 = 20 组 × 2 臂 × 3 次模型调用 ≈ 3–4 元、30–40 分钟；只跑被改任务的单步属后续优化。

---

## 目录结构

```
project/
├─ AGENTS.md              AI 开发规范（唯一权威版本，先读它）
├─ README.md              本文件
├─ pyproject.toml         依赖与工程元数据
├─ .gitignore            忽略规则（密钥、素材、运行产物）
├─ config/
│   ├─ config.toml        默认配置（可入库，零密钥）
│   ├─ .env.example       密钥模板（可入库）
│   └─ .env               真实密钥（本地文件，永不入库）
├─ data/materials/        本地素材库（不入库）
├─ frontend/              Vue 3 单页应用（源码与构建配置）
├─ docs/                  全部文档
├─ evals/                 评测用例与脱敏示例素材包
├─ runs/                  运行产物（不入库）
├─ src/xhs_agent/         源码
└─ tests/                 测试
```

每个目录的职责边界见 `docs/项目结构.md`。

---

## 配置说明

配置分三层，**密钥与源码彻底分离**：

- `config/config.toml`：可公开的默认值，按段组织（`llm` / `vision` / `embedding` / `retrieval` / `match` / `paths` / `database` / `queue` / `frontend`）；
- `config/.env`：只放密钥，被 `.gitignore` 忽略，永不提交；
- 环境变量：优先级最高，用于密钥与部署差异。

**完整的环境变量清单、`config.toml` 段结构、启动前置检查与退出码，见 `docs/contracts/配置契约.md`** —— 本文件不再重复维护那张表，避免两处不一致。

**运行时不降级**：缺密钥、连不上数据库或队列时直接启动失败并给出明确报错。这是刻意取舍——降级模式产出质量差一个档次，被当成正常模式使用反而会损害结果可信度。

---

## 测试与开发规范

```powershell
# 质量门：lint + 类型检查（mypy 严格模式目前覆盖 schemas / config / probe）
uv run ruff check .
uv run mypy

# 全量单元测试
uv run pytest

# 覆盖率（不设门槛，按需查看；加 --cov-report=html 可生成逐行报告）
uv run pytest --cov

# 提交前钩子：装一次即可，之后每次 commit 自动跑上面三条
uv run pre-commit install
uv run pre-commit run --all-files
```

当前 `tests/unit/` 覆盖数据契约、配置加载、启动前置检查、素材索引同步与向量回填，以及 `tools/` 下九个可离线测的模块
（词典 / 匹配 / 检索 / 报告 / 素材 / 媒体 / 模型调用 / 追踪 / 视觉），`uv run pytest` 应全绿。

依赖真实 Postgres / Redis 的集成用例在 `tests/integration/`：默认**不跑**，需要显式触发——

```powershell
uv run pytest -m integration   # 用 testcontainers 起真实容器；需要可用的 Docker
```

Docker 不可用时会直接报错并说明原因（不静默跳过）。

开发前请先读 `AGENTS.md`（AI 开发规范，含目录约定、文件边界、测试要求、依赖策略、提交检查清单）：

- `AGENTS.md` —— 权威规范，动手前必读
- `docs/开发规范.md` —— 同一套规则的详细解释版
- `docs/项目结构.md` —— 目录与文件职责
- `docs/AI开发说明.md` —— 怎么和 AI 协作开发这个项目
- `docs/产品方案.md` —— 产品定位、流程、指标与技术架构
- `docs/简历与面试.md` —— 面向求职的项目描述与面试要点
- `docs/contracts/` —— 五份契约（API / 数据 / 检索 / 配置 / Prompt），唯一事实源
- `docs/adr/` —— 架构决策记录：当初为什么这么选
- `docs/backlog.md` —— 任务分解、里程碑与风险

---

## 路线图

| 阶段 | 内容 |
| --- | --- |
| P1 工程骨架 | 契约重构、配置加载、质量门与首批单测 |
| P2 数据与检索 | Postgres + pgvector、混合召回、评测集 |
| P3 编排与服务 | LangGraph、FastAPI 五个接口（/api）、Celery |
| P4 可观测与交付 | 结构化日志与调用追踪、容器化、CI |
| P5 前端工程 | Vue 3 单页应用：分析台、结果详情、运行历史 + ECharts 可视化 |
| v1.1 及以后 | 多热点横向对比、历史回看、自动抓热榜、效果回流 |

**完整的任务分解、依赖关系、验收标准与工作量估算见 `docs/backlog.md`** —— 本文件只列阶段，不重复维护任务清单。

---

## 许可

暂未选定开源许可，默认保留全部权利。如需开源，请在 `pyproject.toml` 与本节同步补充许可证信息。
