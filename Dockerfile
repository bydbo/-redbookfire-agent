# 多阶段镜像（S4.4）：builder 里用 uv 装依赖，runtime 只带 venv + 源码。
# 同一个镜像服务两种角色：默认 CMD 起 API，compose 里给 worker 覆盖 `command`。
#
# 两条必须遵守的约束（改这个文件前先读）：
# 1. **不能装成 `--no-editable`**：`src/xhs_agent/config.py` 的 `PROJECT_ROOT` 是从自身路径
#    上溯三层算出来的，只有"可编辑安装 + 保留 /app/src"才等于 /app；装进 site-packages 会让
#    配置路径、素材路径、runs 产物路径全部错位。
# 2. runtime 必须保留 `config/config.toml`、`alembic.ini` + `alembic/` 与 `scripts/`：
#    启动前置检查第 2/5 步要读配置与迁移脚本，容器里也要能直接跑索引/回填脚本。
# 镜像里不放密钥、素材与运行产物（由 .dockerignore 挡住 config/.env、data/、runs/）。

FROM ghcr.io/astral-sh/uv:0.12.7 AS uv

FROM python:3.12-slim AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
COPY --from=uv /uv /bin/uv
WORKDIR /app
# 先只拷依赖清单：依赖没变时这一层可复用（改业务代码不会重装依赖）
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
# 再拷源码与运行期资产，补装本项目（可编辑安装，见文件头约束 1）
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY config ./config
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

# 前端构建阶段（S5.9）：node 阶段只产出 frontend/dist，runtime 再把 dist 拷进去。
# 注意：Node 25 已不带可用的 corepack（本机实测直接报错），所以 pnpm 显式装并钉版本。
FROM node:25.3.0-slim AS frontend
WORKDIR /app/frontend
RUN npm install -g pnpm@11.25.0
# 先只拷依赖清单：依赖没变时这一层可复用（改业务代码不会重装依赖）
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml frontend/.node-version ./
RUN pnpm install --frozen-lockfile
# 再拷源码构建（build = type-check + vite build，产物落 /app/frontend/dist）
COPY frontend/ ./
RUN pnpm run build

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/app/.venv/bin:$PATH \
    TZ=Asia/Shanghai
WORKDIR /app
# 非 root 运行；uid/gid 1000 与常见宿主用户一致，bind mount 出来的 runs/ 才写得进去
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home appuser
COPY --from=builder --chown=appuser:appuser /app /app
# 前端产物进镜像：PROJECT_ROOT=/app 与契约默认 dist_dir="frontend/dist" 正好对上，
# 容器按默认 serve=true 即可托管单页应用（S5.9）
COPY --from=frontend --chown=appuser:appuser /app/frontend/dist /app/frontend/dist
USER appuser

EXPOSE 8000

# 默认起 API（推荐入口：先跑 7 步启动前置检查，失败按契约退 2/3）
CMD ["python", "-m", "xhs_agent.serve", "--host", "0.0.0.0", "--port", "8000"]
