# ADR 0002 · 数据库选 PostgreSQL + pgvector

- 状态：已采纳
- 日期：2026-09-30
- 关联：`docs/contracts/数据契约.md`

## 背景

P2 阶段需要把素材索引、运行记录与向量从 JSON 文件搬到数据库。约束是：单机、单用户、素材量级在数万条以内；但项目定位是"企业级 AI 应用"，选型要经得起面试追问。

## 备选方案

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| 保留 JSON 文件 + Chroma 做向量 | 改动最小、零运维 | 没有事务与并发控制；关系查询全靠手写；"企业级"叙事站不住 |
| SQLite + sqlite-vec | 零服务、单文件、部署简单 | 向量扩展生态弱；面试必问"为什么不用 Postgres"，答不上就减分 |
| PostgreSQL 16 + pgvector | 关系与向量同库、事务完整、生态成熟、招聘关键词 | 需要容器；本机资源占用更高 |

## 决策

**选 PostgreSQL 16 + pgvector**，通过 Docker Compose 提供，用 SQLAlchemy 2.0 async + Alembic 管理。

## 后果

**正面**

- 关系数据与向量同库，一次查询就能完成"召回 + 过滤 + 排序"，不需要在应用层合并两套存储；
- pg_trgm 与 pgvector 同库共存，字面与向量两条召回通道可以在 SQL 层面对齐（见 ADR 0003）；
- 事务保证 `runs` / `run_hotspots` / `run_matches` 的写入要么全成功要么全回滚；
- Alembic 迁移让 schema 变更有版本、可回滚、可评审。

**负面**

- 必须启动 Docker Desktop，C 盘空间紧张时需要把镜像存储位置迁到 D 盘；
- 数据库不可用时项目完全不可运行（这是 ADR 0001 的直接后果，属于有意为之）。

**后续**

- compose 中的 Postgres 镜像固定到满足 pgvector ≥ 0.5 的具体 tag，不用 `latest`；
- HNSW 索引参数（m=16、ef_construction=64）在数据量增长后需要重新评估。