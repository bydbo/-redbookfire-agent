# 小红书热点搭子

[![CI](https://github.com/bydbo/-redbookfire-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/bydbo/-redbookfire-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

> **给我一个热点，在你的素材库里找出能蹭上的镜头，并说清为什么能蹭。**

把热点拆成可迁移的**爆点要素**，在你的私有素材库里做**确定性相关性检索**，输出候选镜头、覆盖缺口
和一版文案初稿。文案是副产品，**素材相关性才是真问题**。

---

## 它解决什么问题

热点窗口只有 24–48 小时，创作者的时间却大半花在翻素材上：库里几百上千条、命名靠手写、检索只能靠记忆。

项目做三件事：

1. **拆解**：把热点拆成可迁移的要素（主题 / 场景 / 画面 / 情绪 / 形式 / 人群……）与**不可直用**的要素（明星姓名、肖像、代言暗示）；
2. **检索**：在你的素材库里做双通道召回，输出候选镜头、命中了哪些要素、建议怎么用，以及"缺什么素材"；
3. **撰稿**：给一版初稿（标题备选、封面字、开头 3 秒、正文、话题、剪辑顺序）。

## 一条设计主线：排序不交给大模型

| 谁来做 | 做什么 |
| --- | --- |
| 大模型 | **判断与解释**：拆解要素、解释为什么能蹭、写文案初稿 |
| 确定性算法 | **排序与打分**：双通道召回 → RRF 融合 → 要素类型加权 → Top-K，同分按 id 升序 |

结果可复现、可解释、可评测，成本不随素材量增长。这是整条链路最核心的取舍。

## 已实现的能力

- **对话式入口**：LangGraph `supervisor ⇄ tools` + 原生 Function Calling，7 个工具（跑完整分析 / 解析图片 / 查素材库 / 读历史运行 / 预览爆点要素 / 改写文案 / 按候选写稿），多轮记忆与越界话术；
- **五节点分析状态图**：拆解 → 检索 → 缺口 → 撰稿 → 报告，每步可重试、可上报进度；
- **真流式**：`text/event-stream` + Redis pub/sub，轮次跑在 detached 任务里，关掉页面不打断这一轮；
- **素材库**：上传 / 增量扫描 / 向量回填 / 编辑（库 + 旁车双写）/ 回收站 / 关键帧预览；
- **可观测**：structlog 结构化日志 + Langfuse 调用追踪 + OpenTelemetry（API → DB → worker 一条 trace），阶段快照与调用链可在界面上回看；
- **评测**：20 组人工标注集、检索基线对照、prompt 回归七维度、成本与延迟门槛。

## 架构

```text
浏览器（Vue 3 + Vite + TS）
      │  REST / SSE
      ▼
FastAPI（28 条接口，契约先行）── Celery 投递 ──▶ Celery worker
      │                                            │
      │                                     LangGraph 五节点
      ├──────────────┬─────────────────────────────┤
      ▼              ▼                             ▼
PostgreSQL 16    Redis 7                  LLM（DeepSeek 兼容接口）
+ pgvector      队列 + pub/sub            + 通义 VL / text-embedding-v3
+ pg_trgm
```

## 快速开始

前置：Docker Desktop、Python ≥ 3.11、[uv](https://docs.astral.sh/uv/)、ffmpeg。

```powershell
Copy-Item config/.env.example config/.env      # 填 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY
docker compose up -d --wait                    # api / worker / postgres / redis + 一次性迁移
curl -i http://127.0.0.1:8000/api/health       # db / redis / llm 三项都 ok 才是 200
```

打开 <http://127.0.0.1:8000/> 就是对话页：丢一张图 + 一句话，模型自己决定调哪个工具。

> **不做运行时降级**：缺密钥、连不上数据库或队列时**启动直接失败**并说明原因，不提供"无密钥也能跑"
> 的离线模式——降级产出会被当成正常结果使用，反而损害可信度。
>
> 本地开发（只起依赖、代码用 `uv run` 跑）、素材索引、向量回填、前端调试与全部命令见
> **[开发指南](docs/开发指南.md)**。

## 工程指标（截至 2026-11）

| 指标 | 数值 | 怎么复核 |
| --- | --- | --- |
| Python 源码 | 约 2.9 万行有效代码（`src/`，不含空行与注释） | — |
| 测试 | 2264 条（默认跑 1851 条；413 条真容器集成需 `-m integration`）+ 前端 226 条 | `uv run pytest` / `pnpm test` |
| 覆盖率 | 单测与集成**联合**覆盖率 ≥ 78%，CI 强制 | `uv run python scripts/check_coverage.py` |
| 接口 | 28 条 REST 路径，OpenAPI YAML 是唯一事实源，CI 校验零 diff | `docs/contracts/openapi.yaml` |
| 数据 | 15 个 Alembic 版本，迁移是唯一建表路径 | `uv run alembic history` |
| 交付 | `docker compose up -d --wait` 一条命令起全栈；GitHub Actions 五个 job | `.github/workflows/ci.yml` |
| 真机成本 | 单个热点约 **0.05–0.11 元**、**40–60 秒**（随图片数与模型版本变化） | `evals/reports/` |

## 目录

```text
project/
├─ src/xhs_agent/   Python 包：api / services / workflows / agents / tools / db / core
├─ tests/           单测（离线）与集成（testcontainers 真容器）
├─ frontend/        Vue 3 单页应用
├─ docs/            产品方案、五份契约、ADR、可观测手册、backlog
├─ evals/           评测集、标注与评测报告
├─ config/          config.toml（可公开）+ .env.example（密钥模板）
└─ alembic/         数据库迁移
```

每个目录的职责边界见 [项目结构](docs/项目结构.md)。

## 文档

| 想了解 | 看这里 |
| --- | --- |
| 产品定位、流程与指标 | [产品方案](docs/产品方案.md) |
| 怎么跑、怎么配、怎么改 | [开发指南](docs/开发指南.md) |
| API / 数据 / 检索 / 配置 / prompt | [契约目录](docs/contracts/) |
| 为什么这么选 | [ADR](docs/adr/README.md) |
| 可观测：三层职责、字段字典、排查手册 | [可观测](docs/可观测.md) |
| 任务分解、里程碑与风险 | [backlog](docs/backlog.md) |
| 面向求职的项目描述与面试要点 | [简历与面试](docs/简历与面试.md) |

## 边界与已知限制（如实）

- **本地单机部署**：不提供公网多租户、身份授权与限流；相关能力在 backlog 里登记为未开始；
- **评测规模有限**：20 组人工标注、单轮为主；真实多轮上下文效果（M18）尚未签收，不宣称已达到；
- **提示词不外露**：不通过页面或公开接口提供，模型失败只返回安全错误说明；
- **图片只用于这一次解析**：不落盘、不入素材库，附件落 `runs/_chat/`；
- **旧结果不自动重算**：分析结果是当次运行的快照，改素材不会回写历史分数。

## 许可

[MIT](LICENSE) © 2026 天树归
