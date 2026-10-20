"""E8 浏览器冒烟：真实 FastAPI + Postgres + Redis + 前端 dist + Edge。

场景：打开历史结果页，验证冻结素材标题、已删除素材状态、历史预览、执行/证据面板；
再读取 HTML/Markdown 报告，确认与详情同一历史文字依据。不调用付费模型。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from xhs_agent.config import load_config

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = "pgvector/pgvector:0.8.6-pg16"
REDIS_IMAGE = "redis:7.4.11-alpine"
INIT_DIR = PROJECT_ROOT / "docker" / "postgres" / "init"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, process: subprocess.Popen, log: Path, timeout: float = 40) -> None:
    import httpx

    client = httpx.Client(trust_env=False, timeout=1)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(log.read_text(encoding="utf-8", errors="replace")[-2000:])
        try:
            if client.get(url).status_code < 500:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    raise RuntimeError(f"服务未就绪：{url}")


def seed(factory, cfg, index_dir: Path) -> dict[str, str]:
    from xhs_agent.db.models import Hotspot, Material, Run, RunHotspot, RunMatch
    from xhs_agent.services.run_snapshots import (
        evidence_meta,
        execution_snapshot,
        material_snapshot,
    )

    async def main() -> dict[str, str]:
        media = Path(cfg.materials_dir()) / "sport" / "current.mp4"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"\x00" * 64)
        async with factory() as session:
            run = Run(job_id="e8-browser", status="succeeded", topk=5,
                      prompt_versions={"hotspot_clue": 1})
            hotspot = Hotspot(raw_text="球场夜跑", clue={"why_it_works": ["夜跑反差"]})
            current = Material(path="sport/current.mp4", type="video", title="当前素材",
                               description="当前描述", tags=["夜跑"], elements=[],
                               source="sidecar", keyframes=[], fingerprint="fp-current")
            session.add_all([run, hotspot, current])
            await session.flush()
            frame = index_dir / "keyframes" / "e8" / "kf_0.jpg"
            frame.parent.mkdir(parents=True, exist_ok=True)
            import base64

            frame.write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
                "/x8AAusB9Y9Z9ZkAAAAASUVORK5CYII="))
            current.keyframes = [str(frame)]
            rh = RunHotspot(run_id=run.id, hotspot_id=hotspot.id, position=1,
                            status="succeeded", raw_text="球场夜跑",
                            clue={"hotspot_raw": "球场夜跑", "why_it_works": ["夜跑反差"],
                                  "mechanisms": [], "elements": [], "match_keywords": ["夜跑"]},
                            coverage={"ratio": 1, "covered": [], "gaps": []},
                            gap_advice={"items": []},
                            retrieval_meta={"literal_recalled": 1, "vector_recalled": 0,
                                            "candidates_considered": 2,
                                            "index_summary": {"materials_count": 2,
                                                              "digest": "idx"}},
                            draft={"material_id": str(current.id), "titles": [],
                                   "body": "历史初稿", "tags": []})
            session.add(rh)
            await session.flush()
            deleted_snapshot = {
                **material_snapshot(current), "id": str(uuid.UUID("00000000-0000-4000-8000-000000000001")),
                "path": "sport/deleted.mp4", "title": "已删除历史素材",
                "fingerprint": "fp-deleted", "keyframes": [],
            }
            current_snapshot = material_snapshot(current)
            deleted_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
            session.add_all([
                RunMatch(run_hotspot_id=rh.id, material_id=deleted_id, rank=1, score=0.9,
                         reasons=["旧理由"], hits=[], missing=[], usage="旧用法",
                         material_snapshot=deleted_snapshot,
                         evidence_meta={"candidate": evidence_meta(
                             origin="cutover_capture", source_ref="e6:old",
                             completeness="complete", payload=deleted_snapshot)}),
                RunMatch(run_hotspot_id=rh.id, material_id=current.id, rank=2, score=0.8,
                         reasons=["新理由"], hits=[], missing=[], usage="新用法",
                         material_snapshot=current_snapshot,
                         evidence_meta={"candidate": evidence_meta(
                             origin="runtime", source_ref="candidate",
                             completeness="complete", payload=current_snapshot)}),
            ])
            rh.evidence_meta = {
                "raw_text": evidence_meta(origin="runtime", source_ref="run:e8/p:1",
                                          completeness="complete", payload="球场夜跑"),
                "clue": evidence_meta(origin="runtime", source_ref="run:e8/p:1",
                                      completeness="complete", payload=rh.clue),
                "gap_advice": evidence_meta(origin="runtime", source_ref="run:e8/p:1",
                                            completeness="complete", payload={"items": []}),
                "retrieval": evidence_meta(origin="runtime", source_ref="run:e8/p:1",
                                           completeness="complete", payload=rh.retrieval_meta),
            }
            run.execution_snapshot = execution_snapshot(
                cfg, topk=5,
                prompt_versions={"hotspot_clue": 1})
            run.execution_snapshot["llm"] = {"provider": "fake", "model": "fake-e8-model"}
            run.evidence_meta = {"execution": evidence_meta(
                origin="runtime", source_ref=f"run:{run.id}", completeness="complete",
                payload=run.execution_snapshot)}
            await session.commit()
            return {"run_id": str(run.id), "material_id": str(current.id)}
    return asyncio.run(main())


BROWSER = r"""
const { chromium } = require(process.env.PLAYWRIGHT_MODULE)
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')

;(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.SMOKE_EDGE, headless: true, args: ['--no-proxy-server'],
  })
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } })
  const out = process.env.SMOKE_OUTPUT
  const checks = []
  try {
    await page.goto(`${process.env.SMOKE_ORIGIN}/runs/${process.env.SMOKE_RUN}`, { waitUntil: 'networkidle' })
    await page.waitForFunction(() => document.body.innerText.includes('执行与证据'))
    const body = await page.locator('body').innerText()
    assert.match(body, /fake-e8-model/)
    assert.match(body, /已删除历史素材/)
    assert.match(body, /原素材已删除/)
    assert.match(body, /历史初稿/)
    checks.push('结果页展示执行快照、证据、冻结素材与删除状态')
    const images = page.locator('img')
    const count = await images.count()
    assert.ok(count >= 1, '应至少有一张当前素材历史预览')
    const loaded = await images.first().evaluate((img) => img.complete && img.naturalWidth > 0)
    assert.ok(loaded, '历史预览图未加载')
    checks.push('当前素材历史关键帧可加载')
    await fs.mkdir(out, { recursive: true })
    await page.screenshot({ path: path.join(out, 'run-result.png'), fullPage: true })
    await page.getByText('候选素材（2）').scrollIntoViewIfNeeded()
    await page.screenshot({ path: path.join(out, 'run-candidates.png'), fullPage: false })
    await fs.writeFile(path.join(out, 'result.json'), JSON.stringify({ checks }, null, 2))
    console.log(JSON.stringify({ checks }))
  } catch (err) {
    await fs.mkdir(out, { recursive: true }).catch(() => {})
    await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {})
    console.error(err)
    process.exit(1)
  } finally {
    await browser.close()
  }
})()
"""


def main() -> int:
    playwright = os.environ.get("PLAYWRIGHT_MODULE", "")
    edge = os.environ.get("SMOKE_EDGE", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not playwright or not Path(playwright).exists() or not Path(edge).exists():
        print("缺少 PLAYWRIGHT_MODULE 或 Edge", file=sys.stderr)
        return 2
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = PROJECT_ROOT / "runs" / "_e8" / f"browser-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix="e8-ui-"))
    pg, redis, api, worker = None, None, None, None
    try:
        pg = PostgresContainer(POSTGRES_IMAGE, driver="asyncpg", username="xhs",
                               password="xhs", dbname="xhs")
        pg.with_volume_mapping(str(INIT_DIR), "/docker-entrypoint-initdb.d", "ro")
        redis = RedisContainer(REDIS_IMAGE)
        pg.start()
        redis.start()
        dsn = pg.get_connection_url()
        redis_url = f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"
        materials_dir = temp / "materials"
        materials_dir.mkdir()
        index_dir = temp / "runs" / "_index"
        config = temp / "config.toml"
        config.write_text(
            f'[paths]\nmaterials_dir = "{materials_dir.as_posix()}"\n'
            f'runs_dir = "{(temp / "runs").as_posix()}"\n'
            f'index_dir = "{index_dir.as_posix()}"\n[embedding]\nenabled = false\n',
            encoding="utf-8")
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        env = {**os.environ, "DATABASE_URL": dsn, "REDIS_URL": redis_url,
               "XHS_CONFIG_PATH": str(config), "PYTHONIOENCODING": "utf-8"}
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=PROJECT_ROOT,
                       env=env, check=True, capture_output=True, text=True)
        factory = async_sessionmaker(create_async_engine(dsn, poolclass=NullPool),
                                     expire_on_commit=False)
        cfg = load_config(str(config))
        ids = seed(factory, cfg, index_dir)
        port = free_port()
        launcher = temp / "launch.py"
        launcher.write_text(
            "from xhs_agent.api.main import create_app\napp = create_app(check_startup=False)\n",
            encoding="utf-8")
        api_env = {**env, "PYTHONPATH": str(temp) + os.pathsep + str(PROJECT_ROOT / "src")}
        api_log = open(out / "api.log", "wb")  # noqa: SIM115 - 生命周期跟随子进程
        api = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"), "-u", "-m", "uvicorn",
             "launch:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=str(PROJECT_ROOT), env=api_env, stdout=api_log, stderr=subprocess.STDOUT)
        wait_http(f"http://127.0.0.1:{port}/", api, out / "api.log")
        worker = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv" / "Scripts" / "celery.exe"), "-A",
             "xhs_agent.tasks.worker:app", "worker", "--pool=solo", "--loglevel=warning"],
            cwd=str(PROJECT_ROOT), env=env,
            stdout=open(out / "worker.log", "wb"), stderr=subprocess.STDOUT)  # noqa: SIM115
        script = out / "browser.cjs"
        script.write_text(BROWSER, encoding="utf-8")
        node_env = {**os.environ, "PLAYWRIGHT_MODULE": playwright, "SMOKE_EDGE": edge,
                    "SMOKE_ORIGIN": f"http://127.0.0.1:{port}",
                    "SMOKE_RUN": ids["run_id"], "SMOKE_OUTPUT": str(out)}
        result = subprocess.run(["node", str(script)], env=node_env, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=180)
        (out / "browser.log").write_text((result.stdout or "") + "\n" + (result.stderr or ""),
                                         encoding="utf-8")
        if result.returncode != 0:
            print(result.stderr[-1500:], file=sys.stderr)
            return 1
        import httpx
        client = httpx.Client(trust_env=False, timeout=5)
        md = client.get(f"http://127.0.0.1:{port}/api/runs/{ids['run_id']}/report?format=md").text
        html = client.get(f"http://127.0.0.1:{port}/api/runs/{ids['run_id']}/report").text
        assert "已删除历史素材" in md and "已删除历史素材" in html
        assert "历史初稿" in md and "已删除历史素材" in md
        assert "fake-e8-model" in md   # 报告使用保存的执行配置
        (out / "report.md").write_text(md, encoding="utf-8")
        (out / "report.html").write_text(html, encoding="utf-8")
        print(result.stdout.strip())
        return 0
    finally:
        for process in (worker, api):
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        if pg is not None:
            pg.stop()
        if redis is not None:
            redis.stop()
        (out / "context.json").write_text(json.dumps(
            {"stamp": stamp, "run": None, "note": "E8 read-only browser smoke"},
            ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
