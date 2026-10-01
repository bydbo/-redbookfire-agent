"""compose 配置的静态校验（S2.1）。

真实验收（扩展就位、pgvector 版本达标）靠 `docker compose up` 冒烟，见 `docs/backlog.md` 的 S2.1；
这里只守住"改配置时不会悄悄把镜像 tag 换成 latest、把端口裸暴露"这类回归。
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = PROJECT_ROOT / "docker-compose.yml"
INIT_SQL = PROJECT_ROOT / "docker" / "postgres" / "init" / "01-extensions.sql"

POSTGRES_IMAGE = "pgvector/pgvector:0.8.6-pg16"
REDIS_IMAGE = "redis:7.4.11-alpine"


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
        assert text.count("healthcheck:") == 2
        assert "pg_isready" in text
        assert "redis-cli" in text


class TestInitSql:
    def test_creates_contract_extensions_idempotently(self):
        sql = INIT_SQL.read_text(encoding="utf-8")
        assert "CREATE EXTENSION IF NOT EXISTS vector;" in sql
        assert "CREATE EXTENSION IF NOT EXISTS pg_trgm;" in sql
