"""compose 配置的静态校验（S2.1）。

真实验收（扩展就位、pgvector 版本达标）靠 `docker compose up` 冒烟，见 `docs/backlog.md` 的 S2.1；
这里只守住"改配置时不会悄悄把镜像 tag 换成 latest、把端口裸暴露"这类回归。
S4.4 起 api / worker / migrate 三个服务也在这里守住（命令、健康检查、依赖条件、挂载）。
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = PROJECT_ROOT / "docker-compose.yml"
INIT_SQL = PROJECT_ROOT / "docker" / "postgres" / "init" / "01-extensions.sql"

POSTGRES_IMAGE = "pgvector/pgvector:0.8.6-pg16"
REDIS_IMAGE = "redis:7.4.11-alpine"
APP_IMAGE = "xhs-agent:0.1.0"


class TestCompose:
    def test_declares_pinned_images(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert f"image: {POSTGRES_IMAGE}" in text
        assert f"image: {REDIS_IMAGE}" in text
        assert ":latest" not in text

    def test_declares_both_services_and_volumes(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert "\n  postgres:" in text
        assert "\n  redis:" in text
        assert "\nvolumes:" in text
        assert "pgdata:" in text
        assert "redisdata:" in text

    def test_ports_bind_loopback_only(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert '"127.0.0.1:5432:5432"' in text
        assert '"127.0.0.1:6379:6379"' in text
        # 不允许写成 5432:5432 / 6379:6379（等于对局域网开放）
        assert '"5432:5432"' not in text
        assert '"6379:6379"' not in text

    def test_init_script_is_mounted_read_only(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert "./docker/postgres/init:/docker-entrypoint-initdb.d:ro" in text

    def test_both_services_have_healthcheck(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert text.count("healthcheck:") == 4        # postgres / redis / api / worker
        assert "pg_isready" in text
        assert "redis-cli" in text


class TestAppServices:
    """S4.4：api / worker / migrate 三个应用服务（同一个镜像，命令不同）。"""

    def test_all_services_are_declared(self):
        text = COMPOSE.read_text(encoding="utf-8")
        for service in ("postgres", "redis", "migrate", "api", "worker"):
            assert f"\n  {service}:" in text

    def test_image_is_built_and_pinned(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert f"image: {APP_IMAGE}" in text
        assert "dockerfile: Dockerfile" in text
        assert "context: ." in text
        assert ":latest" not in text

    def test_migrate_runs_alembic_once(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert 'command: ["alembic", "upgrade", "head"]' in text
        assert 'restart: "no"' in text
        assert "service_completed_successfully" in text

    def test_api_and_worker_commands(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert '"python", "-m", "xhs_agent.serve", "--host", "0.0.0.0", "--port", "8000"' in text
        assert '"xhs_agent.tasks.worker:app"' in text
        assert '"--concurrency=2"' in text

    def test_api_port_binds_loopback_only(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert '"127.0.0.1:8000:8000"' in text
        assert '"8000:8000"' not in text

    def test_worker_healthcheck_uses_celery_ping(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert "inspect ping -d celery@$$HOSTNAME" in text

    def test_container_env_overrides_host_addresses(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert "postgresql+asyncpg://xhs:${POSTGRES_PASSWORD:-xhs}@postgres:5432/xhs" in text
        assert "REDIS_URL: redis://redis:6379/0" in text
        assert 'XHS_FRONTEND_SERVE: "false"' in text      # 前端 dist 归 E5 / S5.9
        assert "XHS_LOG_FORMAT: json" in text

    def test_env_file_is_optional_and_points_at_config_env(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert "config/.env" in text
        assert "required: false" in text

    def test_data_and_runs_are_bind_mounted(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert "./data/materials:/app/data/materials" in text
        assert "./runs:/app/runs" in text

    def test_graceful_shutdown_windows(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert "stop_grace_period: 180s" in text          # worker：在飞分析要收尾
        assert "stop_grace_period: 30s" in text           # api


class TestInitSql:
    def test_creates_contract_extensions_idempotently(self):
        sql = INIT_SQL.read_text(encoding="utf-8")
        assert "CREATE EXTENSION IF NOT EXISTS vector;" in sql
        assert "CREATE EXTENSION IF NOT EXISTS pg_trgm;" in sql
