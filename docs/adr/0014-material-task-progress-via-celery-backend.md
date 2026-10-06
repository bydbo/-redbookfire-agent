# ADR 0014 · 素材索引进度用 Celery 结果后端，分析任务显式 ignore_result

- 状态：已采纳
- 日期：2026-10-06
- 关联：`src/xhs_agent/tasks/celery_app.py`、`src/xhs_agent/tasks/indexing.py`、
  `src/xhs_agent/api/deps.py`、`docs/contracts/openapi.yaml`、ADR 0004、ADR 0011

## 背景

S3.4b 定下的口径是「**不设 result backend**」：运行状态的权威源是数据库（ADR 0011），我们从
不读 Celery 结果；而且设了后端之后 `send_task` 会顺带碰后端，broker 不可达时实测要 6.8 秒并
抛一个裸 `RuntimeError("Retry limit exceeded … result store backend")`，把「队列不可用 → 503」
这条语义搞坏。

S6.3 / S6.4 引入素材索引任务（扫描素材库、上传后入库、回收站恢复），它要在界面上回答两个问题：
**跑到哪一步了**、**失败了为什么**。这与本轮「零迁移、不改表结构」的约定冲突：`runs` 表承载不了
素材索引任务的进度，而新加一张任务表要改数据契约、写迁移。

## 备选方案

| 方案 | 代价 |
| --- | --- |
| 新建任务表（或硬塞进 `runs`） | 违背本轮「零迁移」；一次性任务还要自带状态机与清理策略 |
| 不设后端：前端轮询素材列表 | 只能看到「迟迟没出现」，**失败原因只留在 worker 日志里**——与项目「不做降级、错误要明确」的口径相反 |
| 上传时同步索引（请求内跑完） | 长任务回到 API 进程，违背 ADR 0004；大视频上传会长时间占住请求 |
| **设 Redis 结果后端 + 分析任务显式 `ignore_result=True`** | 结果键占一点 Redis 空间（`result_expires=3600`）；护栏要写在投递端与任务声明两处 |

## 决策

- `build_celery_app` 设 `backend = broker = REDIS_URL`（同一个 Redis，不新增容器、不新增配置项）；
- **分析任务显式 `ignore_result=True`**：投递端（`CeleryDispatcher.enqueue`）传
  `ignore_result=True`，任务声明（`register_analyze_task`）同样写 `ignore_result=True`。
  `send_task` 在 `ignore_result=True` 时不调 `backend.on_task_call`，因此 broker 不可达仍然是
  快速 `OperationalError` → 503；worker 侧 `get_actual_ignore_result` 优先取消息头，两边一致就
  不会往 Redis 堆没人读的分析结果；
- 素材索引任务保持默认（存结果）：`GET /api/materials/tasks/{task_id}` 读它的 state 与 meta
  （阶段名 / 计数 / 失败原因），不新建表、不加列。

## 后果

**正面**

- 素材库的导入与扫描在界面上有阶段进度与失败原因，且不动表结构；
- 分析路径的错误语义（快速 503）与 Redis 占用都没变——护栏是显式的两处 `ignore_result=True`。

**负面**

- 结果键有生命周期（1 小时）：过期后 `GET /api/materials/tasks/{id}` 会退回 `PENDING`
  （"排队中或已完成"）——这正是"进度是临时的、素材本身以磁盘与数据库为准"的体现；
- API 进程也要能构造一个 Celery app（复用同一个 `build_celery_app`，不额外读配置）。

**后续**

- 若将来需要"任务历史"，再考虑为异步任务单独建表；
- 「分析任务不落结果」这条护栏任何时候都不要去掉（ADR 0011 的前提）。
