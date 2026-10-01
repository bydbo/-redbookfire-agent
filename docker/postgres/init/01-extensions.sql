-- PostgreSQL 初始化脚本：本地开发与集成测试共用的扩展。
-- 数据契约 §6：扩展需要数据库超级用户权限，因此由 compose 初始化脚本预先创建；
-- Alembic 迁移里只用 IF NOT EXISTS 兜底，downgrade 不 DROP EXTENSION（扩展是同库共享资源）。
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
