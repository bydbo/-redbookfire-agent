# 小红书热点搭子

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
| 爆点要素词典与离线规则引擎 | ✅ 已完成（离线规则引擎将在 P2 删除） |
| 模型调用层（兼容接口 + 结构化输出自修重试） | ✅ 已完成 |
| 素材扫描与索引（旁车文件／文件名／视觉打标，增量更新） | ✅ 已完成 |
| 相关性打分与覆盖度计算 | ✅ 已完成 |
| 报告渲染（HTML + Markdown）与运行追踪 | ✅ 已完成 |
| `agents/` 与 `workflows/` 编排层 | ⬜ 未实现 |
| 本地依赖编排（PostgreSQL 16 + pgvector、Redis，Docker Compose） | ✅ 已完成（S2.1） |
| 集成测试基座（testcontainers + pytest-asyncio） | ✅ 已完成（S2.9） |
| 数据库 ORM 与会话（五张表、14 条索引、向量列） | ✅ 已完成（S2.2） |
| 素材索引入库 + 增量判断（`materials` 表 + 指纹增量 + 消失即删） | ✅ 已完成（S2.4） |
| Alembic 数据库迁移（唯一建表路径） | ✅ 已完成（S2.3） |
| 评测集 v1 与示例素材包（七维度口径、版本冻结） | ✅ 已完成（S2.0） |
| 服务入口（FastAPI）与编排层 | ⬜ 未实现 |
| 前端单页应用（Vue 3 + Vite） | ⬜ 未实现 |

当前可运行的是本地依赖编排、启动前置检查与素材索引入库（`docker compose up -d --wait`、`uv run python -m xhs_agent.probe`、`uv run python scripts/index_materials.py`），业务链路尚未打通。路线图见文末。

---

## 技术栈

已落地的选型（完整清单、依据与规划中的演进见 `docs/技术栈.md`）：

| 层面 | 选型 |
| --- | --- |
| 语言 | Python ≥ 3.11 |
| 包管理与构建 | uv + `pyproject.toml`（hatchling 后端，src 布局） |
| 运行时依赖 | pydantic v2、pydantic-settings、python-dotenv、SQLAlchemy 2.0 async + asyncpg、pgvector、alembic |
| 开发依赖 | pytest、pytest-cov、pytest-asyncio、testcontainers、ruff、mypy、pre-commit |
| 文本模型接入 | OpenAI 兼容 `/chat/completions` 协议（默认 DeepSeek），标准库 `urllib` 直连，JSON mode 结构化输出 + 解析失败自修 |
| 多模态接入 | 通义千问 VL（`qwen-vl-max`），关键帧 base64 内联 |
| 音视频处理 | ffmpeg / ffprobe（用于探测与抽帧；缺失时跳过抽帧，属能力裁剪） |
| 检索算法 | 字符 bigram Jaccard + 要素类型加权；无 embedding、无向量库 |
| 配置 | pydantic-settings + TOML：环境变量 > `.env` > config.toml > 代码默认值；启动前置检查见 `uv run python -m xhs_agent.probe` |
| 数据落地 | PostgreSQL `materials` 表（素材索引，增量同步）+ JSONL 轨迹 + Markdown 与单文件 HTML 报告；旧 JSON 索引保留但标为 legacy |
| 测试 | pytest |

### 最终技术栈（企业级，已确定待落地）

| 层面 | 选型 |
| --- | --- |
| Web 服务 | FastAPI + Uvicorn |
| HTTP 客户端 | httpx（异步、连接池、重试） |
| Agent 编排 | LangGraph（状态图 + 检查点 + 失败重试） |
| 数据库与缓存 | PostgreSQL 16 + pgvector 0.8.6、Redis 7.4.11（本地 `docker compose` 起，tag 固定） |
| 数据访问 | SQLAlchemy 2.0 async ORM + asyncpg + pgvector（已落地，模型见 `src/xhs_agent/db/`）；Alembic 迁移待落地（S2.3） |
| 检索 | 混合召回：pg_trgm 字面 + pgvector 语义，RRF 融合后套要素加权 |
| Embedding | 默认通义 text-embedding-v3（API）；本地小模型为可选实现 |
| 队列与缓存 | Redis + Celery |
| 可观测 | Langfuse Cloud + structlog，run_id 贯穿全链路 |
| 容器化 / CI | Docker Compose（api / worker / postgres / redis）；GitHub Actions |
| 代码质量 / 测试 | ruff + mypy + pre-commit + pytest-cov + pytest-asyncio + testcontainers（均已落地） |
| 前端 | Vue 3 + Vite + TypeScript + Pinia + Vue Router |
| UI 与可视化 | Naive UI + Tailwind CSS + ECharts |
| 接口类型 | `openapi-typescript` 从契约生成 TS 类型 |

以上组件**尚未落地**，落地顺序见 `docs/技术栈.md` 第四节。**简历只写已经落地的技术栈。**

> **运行时降级已取消**：缺密钥、连不上数据库或队列时启动直接失败并明确报错，不再提供“无密钥也能跑”的离线模式。

## 快速开始

前置条件：Python ≥ 3.11、`uv`、`ffmpeg` / `ffprobe`（用于抽帧）。

> **注意**：下面是当前代码可运行的步骤。本地依赖（PostgreSQL 16 + pgvector、Redis）已由 `docker-compose.yml` 提供；服务化（FastAPI / Celery）仍在后续阶段。

```powershell
# 1. 安装环境（首次需要网络）
uv sync

# 2. 起本地依赖（PostgreSQL 16 + pgvector、Redis；镜像 tag 固定在 docker-compose.yml）
docker compose up -d --wait

# 3. 准备配置（必须填必填项：项目不做无密钥降级）
Copy-Item config/.env.example config/.env
#    编辑 config/.env 填 DEEPSEEK_API_KEY（P1 必填）与 DATABASE_URL（P2 必填，
#    本地默认 postgresql+asyncpg://xhs:xhs@localhost:5432/xhs）；
#    DASHSCOPE_API_KEY 按能力启用时再填，REDIS_URL 到 P3 才必填

# 4. 建表（Alembic 迁移；改模型后必须同时提交迁移脚本）
uv run alembic upgrade head
#    回滚与一致性自查：uv run alembic downgrade base / uv run alembic check

# 5. 素材索引入库（可选，首次使用素材库时跑一次全量；之后每次分析前自动增量）
#    把素材放进 data/materials/ 后执行；--max-vision-items 控制视觉打标的成本上限
uv run python scripts/index_materials.py

# 6. 验证安装与本地依赖（两个服务应是 healthy）
uv run python -c "import xhs_agent; print(xhs_agent.__version__)"
docker compose ps

# 7. 验证配置读取（输出不应包含任何密钥）
uv run python -c "from xhs_agent.config import load_config; print(load_config().describe())"

# 8. 跑启动前置检查（缺必填项会打印 E_CONFIG_MISSING 并以退出码 2 结束）
uv run python -m xhs_agent.probe
```

网络受限时：`uv sync --no-dev` 只装运行时环境——当前运行时依赖只有 pydantic、pydantic-settings 与 python-dotenv；不过 `import xhs_agent` 仍然需要安装或设置 `PYTHONPATH=src`（src 布局）。

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

当前 `tests/unit/` 覆盖数据契约、配置加载、启动前置检查，以及 `tools/` 下八个可离线测的模块
（词典 / 打分 / 报告 / 素材 / 媒体 / 模型调用 / 追踪 / 视觉），`uv run pytest` 应全绿。

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
- `docs/contracts/` —— 四份契约（API / 数据 / 检索 / 配置），唯一事实源
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
