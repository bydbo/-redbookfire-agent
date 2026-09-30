# ADR 0004 · 任务队列选 Celery 而非 Arq

- 状态：已采纳
- 日期：2026-09-30
- 关联：`docs/contracts/openapi.yaml`（`/analyze` 的异步模型）、`docs/contracts/配置契约.md` `[queue]` 段

## 背景

素材索引与热点分析都是长任务（数十秒到数分钟），接口必须异步化：`POST /analyze` 立即返回 `job_id`，前端轮询状态。需要一个任务队列来承接，broker 与 result backend 复用 Redis。

服务本身是 async FastAPI，因此候选里既有原生异步的队列，也有传统的同步队列。

## 备选方案

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| FastAPI `BackgroundTasks` | 零依赖、零运维 | 随进程重启丢任务；无法水平扩展；不能算"分布式任务队列"，企业叙事不足 |
| Arq | async 原生，与 FastAPI 契合；代码量少 | 社区小、生态薄；招聘关键词覆盖低；生产案例少 |
| Celery | 生态最大、招聘关键词覆盖率最高；支持重试、路由、限流、超时、监控 | 同步模型与 async 服务有阻抗；需要额外 worker 进程；配置项多 |
| Dramatiq / RQ | 比 Celery 更轻 | 关键词覆盖同样不如 Celery |

## 决策

**选 Celery + Redis**（broker 与 result backend 均使用 Redis）。Celery worker 作为独立进程运行，由 `docker compose` 统一编排。

## 后果

**正面**

- 任务自带重试、超时、状态回写能力，与 `runs` 表的状态机自然对齐；
- 面试与简历上"Celery + Redis + 异步任务"是清晰的加分项；
- 后续要做定时任务（如定期重建索引）时不需要换技术。

**负面**

- Celery 是同步执行模型，在任务内部调用异步代码需要一个明确策略（每个任务内自建事件循环或使用同步 HTTP 客户端），这层适配必须在 P3 一次性定好，不能每个任务各写一套；
- 排查问题比"进程内后台任务"复杂，`trace.jsonl` 与 Langfuse 的可观测价值更高。

**后续**

- P3 落地时明确"任务函数内部如何调用 async 依赖"的统一写法，并写进 `docs/开发规范.md`；
- `[queue]` 段的软超时与硬超时必须配置，避免任务永久挂起。