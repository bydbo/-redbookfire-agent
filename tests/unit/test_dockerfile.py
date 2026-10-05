"""Dockerfile 与 .dockerignore 的静态校验（S4.4）。

真实验收是 `docker compose up -d --wait` 冒烟（见 `docs/backlog.md` 的 S4.4）；
这里守住几条改了就很难发现的回归：基础镜像没钉版本、镜像里混进密钥或素材、装成非可编辑。
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"


class TestDockerfile:
    def test_is_multi_stage_with_pinned_bases(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert text.count("\nFROM ") == 4           # uv / builder / frontend / runtime（S5.9）
        assert "FROM python:3.12-slim AS builder" in text
        assert "FROM python:3.12-slim AS runtime" in text
        assert "FROM node:25.3.0-slim AS frontend" in text   # 前端构建阶段也钉版本
        assert "ghcr.io/astral-sh/uv:0.12.7" in text       # uv 也要钉版本
        assert ":latest" not in text

    def test_installs_dependencies_frozen_and_keeps_project_editable(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert "uv sync --frozen --no-dev --no-install-project" in text
        assert "uv sync --frozen --no-dev" in text
        # 绝不能装成不可编辑：PROJECT_ROOT 依赖 /app/src 这个布局（只查 RUN 行，别误伤注释）
        run_lines = [line for line in text.splitlines() if line.startswith("RUN ")]
        assert not any("--no-editable" in line for line in run_lines)

    def test_runtime_keeps_preflight_assets(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        for keep in ("COPY alembic ./alembic", "COPY alembic.ini ./", "COPY config ./config",
                     "COPY src ./src", "COPY scripts ./scripts"):
            assert keep in text

    def test_runs_as_non_root(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert "useradd --uid 1000 --gid 1000" in text
        assert "\nUSER appuser" in text
        assert "COPY --from=builder --chown=appuser:appuser /app /app" in text

    def test_frontend_stage_builds_dist_and_runtime_copies_it(self):
        """S5.9：node 阶段产出 dist，runtime 把 dist 拷进去，容器才托管单页应用。"""
        text = DOCKERFILE.read_text(encoding="utf-8")
        # Node 25 没有可用的 corepack，pnpm 必须显式装并钉版本
        assert "npm install -g pnpm@11.25.0" in text
        assert "pnpm install --frozen-lockfile" in text
        assert "RUN pnpm run build" in text
        assert ("COPY --from=frontend --chown=appuser:appuser /app/frontend/dist "
                "/app/frontend/dist" in text)

    def test_default_command_starts_the_api(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert ('CMD ["python", "-m", "xhs_agent.serve", "--host", "0.0.0.0", "--port", "8000"]'
                in text)
        assert "EXPOSE 8000" in text


class TestDockerignore:
    def test_never_ships_secrets_data_or_runtime_artifacts(self):
        text = DOCKERIGNORE.read_text(encoding="utf-8")
        for pattern in ("config/.env", "data/", "runs/", "evals/", ".git", ".venv"):
            assert f"\n{pattern}\n" in f"\n{text}"

    def test_keeps_env_example_template(self):
        text = DOCKERIGNORE.read_text(encoding="utf-8")
        assert "!config/.env.example" in text

    def test_frontend_source_is_allowed_but_build_artifacts_are_not(self):
        """S5.9：前端源码要进构建上下文（node 阶段用），依赖与产物不进。"""
        text = DOCKERIGNORE.read_text(encoding="utf-8")
        assert "\nfrontend/\n" not in f"\n{text}"        # 不再整块挡掉 frontend/
        for pattern in ("frontend/node_modules/", "frontend/dist/", "frontend/coverage/"):
            assert f"\n{pattern}\n" in f"\n{text}", pattern
