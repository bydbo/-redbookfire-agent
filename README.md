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
| 数据契约（要素／线索／素材／匹配／文案） | ✅ 已完成 |
| 爆点要素词典与离线规则引擎 | ✅ 已完成（离线规则引擎将在 P2 删除） |
| 模型调用层（兼容接口 + 结构化输出自修重试） | ✅ 已完成 |
| 素材扫描与索引（旁车文件／文件名／视觉打标，增量更新） | ✅ 已完成 |
| 相关性打分与覆盖度计算 | ✅ 已完成 |
| 报告渲染（HTML + Markdown）与运行追踪 | ✅ 已完成 |
| `agents/` 与 `workflows/` 编排层 | ⬜ 未实现 |
| 命令行入口 | ⬜ 未实现 |

因此目前**没有可直接运行的业务命令**，下面的快速开始只覆盖环境安装与导入验证。路线图见文末。

---

## 技术栈

已落地的选型（完整清单、依据与规划中的演进见 `docs/技术栈.md`）：

| 层面 | 选型 |
| --- | --- |
| 语言 | Python ≥ 3.11 |
| 包管理与构建 | uv + `pyproject.toml`（hatchling 后端，src 布局） |
| 运行时依赖 | 无，仅标准库 |
| 开发依赖 | pytest |
| 文本模型接入 | OpenAI 兼容 `/chat/completions` 协议（默认 DeepSeek），标准库 `urllib` 直连，JSON mode 结构化输出 + 解析失败自修 |
| 多模态接入 | 通义千问 VL（`qwen-vl-max`），关键帧 base64 内联 |
| 音视频处理 | ffmpeg / ffprobe（用于探测与抽帧；缺失时跳过抽帧，属能力裁剪） |
| 检索算法 | 字符 bigram Jaccard + 要素类型加权；无 embedding、无向量库 |
| 配置 | TOML + `.env` + 环境变量，三层覆盖 |
| 数据落地 | JSON 索引 / JSONL 轨迹 / Markdown 与单文件 HTML 报告；无数据库 |
| 测试 | pytest |

### 最终技术栈（企业级，已确定待落地）

| 层面 | 选型 |
| --- | --- |
| Web 服务 | FastAPI + Uvicorn |
| 数据契约与配置 | Pydantic v2 + pydantic-settings |
| HTTP 客户端 | httpx（异步、连接池、重试） |
| Agent 编排 | LangGraph（状态图 + 检查点 + 失败重试） |
| 数据库 | PostgreSQL 16 + pgvector（Docker Compose 提供） |
| 数据访问 | SQLAlchemy 2.0 async + asyncpg + Alembic |
| 检索 | 混合召回：pg_trgm 字面 + pgvector 语义，RRF 融合后套要素加权 |
| Embedding | 默认通义 text-embedding-v3（API）；本地小模型为可选实现 |
| 队列与缓存 | Redis + Celery |
| 可观测 | Langfuse Cloud + structlog，run_id 贯穿全链路 |
| 容器化 / CI | Docker Compose（api / worker / postgres / redis）；GitHub Actions |
| 代码质量 / 测试 | ruff + mypy + pre-commit；pytest + pytest-asyncio + pytest-cov + testcontainers |

以上组件**尚未落地**，落地顺序见 `docs/技术栈.md` 第四节。**简历只写已经落地的技术栈。**

> **运行时降级已取消**：缺密钥、连不上数据库或队列时启动直接失败并明确报错，不再提供“无密钥也能跑”的离线模式。

## 快速开始

前置条件：Python ≥ 3.11、`uv`、`ffmpeg` / `ffprobe`（用于抽帧）。

> **注意**：下面是当前代码可运行的步骤。企业化改造（服务化、数据库、队列）尚未落地，改造后启动方式会变成 `docker compose up`。

```powershell
# 1. 安装环境（首次需要网络）
uv sync

# 2. 准备配置（必须填密钥：项目不做无密钥降级）
Copy-Item config/.env.example config/.env
#    编辑 config/.env 填入 DEEPSEEK_API_KEY 与 DASHSCOPE_API_KEY

# 3. 验证安装
uv run python -c "import xhs_agent; print(xhs_agent.__version__)"

# 4. 验证配置读取（输出不应包含任何密钥）
uv run python -c "from xhs_agent.config import load_config; print(load_config().describe())"
```

网络受限时：`uv sync --no-dev` 只装运行时环境——当前运行时依赖为空，只有标准库；不过 `import xhs_agent` 仍然需要安装或设置 `PYTHONPATH=src`（src 布局）。

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
├─ docs/                  全部文档
├─ evals/                 评测用例与脱敏示例素材包
├─ runs/                  运行产物（不入库）
├─ src/xhs_agent/         源码
└─ tests/                 测试
```

每个目录的职责边界见 `docs/项目结构.md`。

---

## 配置说明

配置分两层，**密钥与源码彻底分离**：

- `config/config.toml`：可公开的默认值，四个段——`llm`（模型与端点）、`vision`（多模态打标）、`match`（Top-K、阈值、要素类型权重）、`paths`（素材库与产物目录）。
- `config/.env`：只放密钥，被 `.gitignore` 忽略，永不提交。

支持的环境变量（优先级高于配置文件）：

| 变量 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 文本模型密钥（默认走 DeepSeek 兼容接口） |
| `DASHSCOPE_API_KEY` | 多模态模型密钥（素材抽帧打标） |
| `XHS_LLM_PROVIDER` | 覆盖模型供应方（`openai_compatible`；`auto` / `offline` 的降级语义将在改造中移除） |
| `XHS_LLM_BASE_URL` | 覆盖接口地址 |
| `XHS_LLM_MODEL` | 覆盖模型名 |
| `XHS_LLM_API_KEY` | 覆盖密钥 |

改造落地后，这里还会增加 `DATABASE_URL`、`REDIS_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_HOST`——完整契约见 `docs/技术栈.md` 第三节。

**运行时不降级**：缺密钥、连不上数据库或队列时直接启动失败并给出明确报错。这是刻意取舍——降级模式产出质量差一个档次，被当成正常模式使用反而会损害结果可信度。

---

## 测试与开发规范

```powershell
uv run pytest
```

当前 `tests/` 为空目录占位：`uv run pytest` 报"0 用例、退出码 5"属于预期状态。第一个功能落地前必须先补测试。

开发前请先读 `AGENTS.md`（AI 开发规范，含目录约定、文件边界、测试要求、依赖策略、提交检查清单）：

- `AGENTS.md` —— 权威规范，动手前必读
- `docs/开发规范.md` —— 同一套规则的详细解释版
- `docs/项目结构.md` —— 目录与文件职责
- `docs/AI开发说明.md` —— 怎么和 AI 协作开发这个项目
- `docs/产品方案.md` —— 产品定位、流程、指标与技术架构
- `docs/简历与面试.md` —— 面向求职的项目描述与面试要点

---

## 路线图

| 版本 | 内容 |
| --- | --- |
| v1 | 编排层与命令行入口；单热点跑通"拆解 → 检索 → 覆盖缺口 → 文案初稿 → 报告"全链路；20 组标注评测集与基线对比 |
| v1.1 | 多热点横向对比、历史运行回看、素材复用记录、自定义要素词典、旁车文件模板 |
| v2 | 自动抓热榜、发布后排期、效果回流、剪映草稿导出 |

---

## 许可

暂未选定开源许可，默认保留全部权利。如需开源，请在 `pyproject.toml` 与本节同步补充许可证信息。
