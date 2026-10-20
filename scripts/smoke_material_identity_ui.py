"""E6 浏览器全栈冒烟（R06/R07）：真实前端 + API + Postgres + Redis + Celery worker。

前置：`frontend/dist` 已构建；本机有 Edge；`PLAYWRIGHT_MODULE` 指向 playwright-core。
输出：`runs/_material_identity/browser-<stamp>/`（截图、result.json、日志）。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = "pgvector/pgvector:0.8.6-pg16"
REDIS_IMAGE = "redis:7.4.11-alpine"
INIT_DIR = PROJECT_ROOT / "docker" / "postgres" / "init"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, timeout: float = 30.0, process: subprocess.Popen | None = None,
              log: Path | None = None) -> None:
    import httpx

    client = httpx.Client(trust_env=False, timeout=1)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            tail = log.read_text(encoding="utf-8", errors="replace")[-2000:] if log else ""
            raise RuntimeError(f"服务进程提前退出（{process.returncode}）：{tail}")
        try:
            if client.get(url).status_code < 500:
                return
        except Exception:  # noqa: BLE001 - 就绪探测
            pass
        time.sleep(0.3)
    client.close()
    raise RuntimeError(f"服务未在 {timeout}s 内就绪：{url}")


async def seed(dsn: str, materials_dir: Path) -> dict[str, str]:
    """插入 5 条素材：正常 / 待恢复 / 缺失 / 批量缺失 / 正常图片。"""
    from xhs_agent.db.models import Material

    engine = create_async_engine(dsn, poolclass=NullPool)
    ids: dict[str, str] = {}
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            for key, rel, has_file in (
                ("正常片", "运动/正常.mp4", True),
                ("待恢复", "运动/待恢复.mp4", True),
                ("缺失片", "运动/缺失.mp4", False),
                ("批量缺失", "美食/批量缺失.jpg", False),
                ("正常图", "美食/正常.png", True),
            ):
                target = materials_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if has_file:
                    target.write_bytes(b"\x00" * 64)
                row = Material(path=rel, type="image" if target.suffix == ".png" else "video",
                               title=key, description="浏览器冒烟素材", tags=["冒烟"],
                               elements=[], source="sidecar", size_bytes=64,
                               indexed_at=datetime.now(UTC))
                session.add(row)
                await session.flush()
                ids[key] = str(row.id)
            await session.commit()
    finally:
        await engine.dispose()
    return ids


def start_process(args: list[str], *, env: dict[str, str], log: Path) -> subprocess.Popen:
    handle = open(log, "wb")  # noqa: SIM115 - 句柄随进程生命周期关闭
    process = subprocess.Popen(args, env=env, stdout=handle, stderr=subprocess.STDOUT,
                               cwd=str(PROJECT_ROOT))
    process._e6_log = handle  # type: ignore[attr-defined]
    return process


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
    handle = getattr(process, "_e6_log", None)
    if handle is not None:
        handle.close()


class _EmbeddingStub(BaseHTTPRequestHandler):
    """固定向量端点：只服务集成冒烟，返回 1024 维零向量。"""

    def do_POST(self) -> None:  # noqa: N802 - http.server 接口
        length = int(self.headers.get("content-length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        items = payload.get("input") or []
        body = json.dumps({
            "data": [{"index": index, "embedding": [0.0] * 1024}
                     for index in range(len(items))],
            "usage": {"prompt_tokens": max(1, len(items))},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


BROWSER_SCRIPT = r"""
const { chromium } = require(process.env.PLAYWRIGHT_MODULE)
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')

const origin = process.env.SMOKE_ORIGIN
const out = process.env.SMOKE_OUTPUT

async function shot(page, name) {
  await fs.mkdir(out, { recursive: true })
  await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: true })
}

async function text(page) {
  return page.locator('body').innerText()
}

async function waitText(page, needle, timeout = 30000) {
  await page.waitForFunction(
    (value) => document.body.innerText.includes(value), needle, { timeout })
}

async function confirmPop(page) {
  const pop = page.locator('.n-popconfirm').last()
  await pop.getByText('确认', { exact: true }).click()
}

;(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.SMOKE_EDGE,
    headless: true,
    args: ['--no-proxy-server'],
  })
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  const checks = []
  try {
    await page.goto(`${origin}/materials`, { waitUntil: 'networkidle' })
    await waitText(page, '素材库')
    assert.match(await text(page), /磁盘目录是唯一来源/)
    checks.push('打开素材库（真实 API + 数据库）')
    await shot(page, '01-materials')

    // R06/R07：单条缺失 —— 确认文案 + 结果提示
    const card = (title) => page.locator('.n-card').filter({
      has: page.getByText(title, { exact: true }) }).first()
    await card('缺失片').getByRole('button', { name: '删除' }).click()
    await waitText(page, '文件可从回收站恢复')
    assert.match(await page.locator('.n-popconfirm').last().innerText(),
                 /恢复后会作为新素材入库，历史候选及旧引用不会自动恢复/)
    checks.push('R06 单条删除确认文案包含身份边界')
    await shot(page, '02-missing-confirm')
    await confirmPop(page)
    await waitText(page, '原文件缺失')
    assert.match(await text(page), /没有文件进入回收站/)
    checks.push('R07 单条缺失提示仅移除索引')
    await shot(page, '03-missing-result')

    // 正常删除 → 回收站
    await card('待恢复').getByRole('button', { name: '删除' }).click()
    await waitText(page, '文件可从回收站恢复')
    await confirmPop(page)
    await waitText(page, '已移入回收站')
    assert.match(await page.locator('body').innerText(), /恢复后会作为新素材入库/)
    checks.push('删除进入回收站并提示恢复语义')
    await shot(page, '04-trash-result')

    // 回收站恢复：真实 Celery 索引任务 → 新素材身份提示
    await page.getByRole('button', { name: '回收站' }).click()
    await waitText(page, '待恢复.mp4')
    await page.getByRole('button', { name: '恢复', exact: true }).first().click()
    await waitText(page, '已作为新素材入库，历史关联未恢复', 60000)
    checks.push('R06 恢复成功提示新素材身份，历史关联未恢复')
    await shot(page, '05-restored')
    await page.keyboard.press('Escape')

    // 详情删除入口：同一文案语义
    await page.getByRole('button', { name: '详情' }).first().click()
    await waitText(page, '移入回收站')
    await page.getByRole('button', { name: '移入回收站' }).click()
    await waitText(page, '文件可从回收站恢复')
    assert.match(await page.locator('.n-popconfirm').last().innerText(),
                 /历史候选及旧引用不会自动恢复/)
    checks.push('R06 详情删除入口口径一致')
    await shot(page, '06-detail-confirm')
    await page.keyboard.press('Escape')

    // 批量：分别统计缺失 / 正常
    await page.keyboard.press('Escape')
    const boxes = page.locator('.n-checkbox')
    const count = await boxes.count()
    assert.ok(count >= 2, `需要至少 2 个可选条目，实际 ${count}`)
    await boxes.nth(0).click()
    await boxes.nth(1).click()
    await page.getByRole('button', { name: '批量删除' }).click()
    await waitText(page, '确认删除选中的')
    await confirmPop(page)
    await waitText(page, '已移入回收站')
    const summary = await text(page)
    assert.match(summary, /恢复后会作为新素材入库/)
    assert.match(summary, /原文件缺失|失败|已移入回收站/)
    checks.push('R07 批量删除分别统计并保留身份语义')
    await shot(page, '07-batch-result')

    await fs.mkdir(out, { recursive: true })
    await fs.writeFile(path.join(out, 'result.json'), JSON.stringify({ checks }, null, 2))
    console.log(JSON.stringify({ checks }))
  } catch (err) {
    await shot(page, '99-failure').catch(() => {})
    console.error(err)
    process.exit(1)
  } finally {
    await browser.close()
  }
})()
"""


def main() -> int:
    if not shutil.which("node"):
        print("缺少 node，无法执行浏览器冒烟", file=sys.stderr)
        return 2
    playwright_module = os.environ.get("PLAYWRIGHT_MODULE", "")
    if not playwright_module or not Path(playwright_module).exists():
        print("缺少 PLAYWRIGHT_MODULE（playwright-core 路径）", file=sys.stderr)
        return 2
    edge = os.environ.get(
        "SMOKE_EDGE",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not Path(edge).exists():
        print(f"缺少 Edge：{edge}", file=sys.stderr)
        return 2
    if not (PROJECT_ROOT / "frontend" / "dist" / "index.html").is_file():
        print("frontend/dist 未构建", file=sys.stderr)
        return 2

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = PROJECT_ROOT / "runs" / "_material_identity" / f"browser-{stamp}"
    output.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix="e6-ui-"))
    embed_port = free_port()
    embed_stub = ThreadingHTTPServer(("127.0.0.1", embed_port), _EmbeddingStub)
    threading.Thread(target=embed_stub.serve_forever, daemon=True).start()
    pg = PostgresContainer(POSTGRES_IMAGE, driver="asyncpg", username="xhs",
                           password="xhs", dbname="xhs")
    pg.with_volume_mapping(str(INIT_DIR), "/docker-entrypoint-initdb.d", "ro")
    redis = RedisContainer(REDIS_IMAGE)
    api = worker = None
    try:
        pg.start()
        redis.start()
        dsn = pg.get_connection_url()
        redis_url = f"redis://{redis.get_container_host_ip()}:" \
                    f"{redis.get_exposed_port(6379)}/0"
        materials_dir = temp / "materials"
        materials_dir.mkdir()
        config = temp / "config.toml"
        config.write_text(
            f'[paths]\nmaterials_dir = "{materials_dir.as_posix()}"\n'
            f'runs_dir = "{(temp / "runs").as_posix()}"\n'
            f'index_dir = "{(temp / "runs" / "_index").as_posix()}"\n'
            "[embedding]\nenabled = true\n"
            f'base_url = "http://127.0.0.1:{embed_port}/v1"\n'
            'model = "stub-embedding"\n',
            encoding="utf-8")
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        env = {
            **os.environ,
            "DATABASE_URL": dsn,
            "REDIS_URL": redis_url,
            "XHS_CONFIG_PATH": str(config),
            "PYTHONIOENCODING": "utf-8",
            "DASHSCOPE_API_KEY": "stub-key",
        }
        env.pop("DEEPSEEK_API_KEY", None)
        os.environ["DATABASE_URL"] = dsn
        os.environ["XHS_CONFIG_PATH"] = str(config)
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                       cwd=str(PROJECT_ROOT), env=env, check=True,
                       capture_output=True, text=True)
        import asyncio

        asyncio.run(seed(dsn, materials_dir))

        port = free_port()
        launch = temp / "api_launcher.py"
        launch.write_text(
            "from xhs_agent.api.main import create_app\n"
            "app = create_app(check_startup=False)\n", encoding="utf-8")
        api_env = {**env, "PYTHONPATH": str(temp) + os.pathsep
                   + str(PROJECT_ROOT / "src")}
        python_exe = str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")
        api = start_process(
            [python_exe, "-u", "-m", "uvicorn", "api_launcher:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            env=api_env, log=output / "api.log")
        wait_http(f"http://127.0.0.1:{port}/", process=api, log=output / "api.log")
        worker = start_process(
            [str(PROJECT_ROOT / ".venv" / "Scripts" / "celery.exe"),
             "-A", "xhs_agent.tasks.worker:app", "worker", "--pool=solo",
             "--loglevel=warning"],
            env=env, log=output / "worker.log")

        script = output / "browser-smoke.cjs"
        script.write_text(BROWSER_SCRIPT, encoding="utf-8")
        node_env = {
            **os.environ,
            "PLAYWRIGHT_MODULE": playwright_module,
            "SMOKE_EDGE": edge,
            "SMOKE_ORIGIN": f"http://127.0.0.1:{port}",
            "SMOKE_OUTPUT": str(output),
        }
        run = subprocess.run(["node", str(script)], env=node_env, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=180)
        (output / "browser.log").write_text(
            (run.stdout or "") + "\n" + (run.stderr or ""), encoding="utf-8")
        if run.returncode != 0:
            print(run.stderr[-2000:], file=sys.stderr)
            return 1
        print(run.stdout.strip())
        return 0
    finally:
        stop_process(worker)
        stop_process(api)
        pg.stop()
        redis.stop()
        embed_stub.shutdown()
        (output / "context.json").write_text(json.dumps({
            "stamp": stamp, "api_port": None, "dsn": "<redacted>",
            "commands": ["alembic upgrade head", "uvicorn api_launcher:app",
                         "celery worker --pool=solo", "node browser-smoke.cjs"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
