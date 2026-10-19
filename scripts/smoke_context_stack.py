"""真实浏览器、API、Postgres、Redis 的 E7 冒烟；仅模型传输使用固定样本，不调用付费服务。"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from eval_context_sessions import (
    FixedModel,
    fixture_clue,
    fixture_draft,
    runtime_clients,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from xhs_agent.api.deps import get_config, get_dispatcher, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, EnvView
from xhs_agent.core.tracing import get_tracer, reset_tracer
from xhs_agent.db import Base
from xhs_agent.db.models import ChatMessage, ChatSession, Hotspot, Run, RunHotspot
from xhs_agent.services import artifact_versions, chat_turns
from xhs_agent.services.chat import append_message, create_session, save_attachment

ROOT = Path(__file__).resolve().parents[1]


async def execute(args, postgres: str, redis_url: str) -> int:
    output = Path(args.out_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig()
    cfg.paths.runs_dir = str(output / "business")
    cfg.frontend.serve = False
    cfg.context.mode = "enforce"
    cfg.context.model_window_verified = True
    cfg.context.model_context_window = 393216
    cfg.llm.stream_usage_supported = True
    cfg.llm.max_retries = 0
    cfg.embedding.enabled = False
    cfg.vision.enabled = True
    cfg._env = EnvView({}, {"DATABASE_URL": postgres, "REDIS_URL": redis_url,
                           cfg.llm.api_key_env: "fixture", cfg.vision.api_key_env: "fixture"})
    for section in (cfg.llm, cfg.vision, cfg.embedding):
        section._env = cfg._env
    reset_tracer()
    get_tracer(cfg)
    engine = create_async_engine(postgres)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        sid = uuid.UUID((await create_session(db, title="版本与线索回归"))["session_id"])
        other = (await create_session(db, title="另一条对话"))["session_id"]
        source = await append_message(db, sid, role="user", content="固定来源：不要使用感叹号。")
        data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+yN4sAAAAASUVORK5CYII=")
        attachment = save_attachment(cfg, sid, data, name="fixture.png", mime="image/png")
        await append_message(db, sid, role="user", content="原图", attachments=[attachment])
        from xhs_agent.services.image_cache import group_digest
        clue_key = "clue:images:" + group_digest([attachment["sha256"]])
        run, hotspot = Run(job_id=uuid.uuid4().hex, status="succeeded"), Hotspot(raw_text="夜跑", clue=fixture_clue())
        db.add_all([run, hotspot])
        await db.flush()
        db.add(RunHotspot(run_id=run.id, hotspot_id=hotspot.id, position=1, status="succeeded",
                          draft=fixture_draft("D0 历史初稿正文"), coverage={"ratio": 0, "covered": [], "gaps": []}))
        await db.commit()
        await append_message(db, sid, role="assistant", content="初稿已生成", run_id=str(run.id))
        draft_key = f"draft:{run.id}:1"
        d0 = await artifact_versions.ensure_initial(db, session_id=sid, object_key_value=draft_key,
            object_type="draft", run_id=str(run.id), position=1, content=fixture_draft("D0 历史初稿正文"),
            source={"message_id": source["message_id"]})
        await artifact_versions.register_version(db, session_id=sid, object_key_value=draft_key,
            object_type="draft", content=fixture_draft("D1 最新文案正文"), source={"actor": "fixture"})
        await artifact_versions.ensure_initial(db, session_id=sid, object_key_value=clue_key,
            object_type="clue", run_id=None, position=None, content={"raw_text": "C0 公园夜跑",
                "clue": fixture_clue(), "edited_fields": []}, source={"message_id": source["message_id"]})
        legacy = ChatSession(title="历史恢复回归", context_state={})
        db.add(legacy)
        await db.flush()
        for index in range(21):
            db.add(ChatMessage(session_id=legacy.id, role="user", status="succeeded",
                               content=f"历史记录{index}", created_at=datetime(2020, 1, 1, tzinfo=UTC)))
        await db.commit()
        manifest = {"session_id": str(sid), "other_session_id": other, "legacy_session_id": str(legacy.id),
                    "draft_v1": str(d0.id), "clue_key": clue_key}
    app = create_app(check_startup=False)
    app.dependency_overrides[get_config] = lambda: cfg

    async def database():
        async with factory() as db:
            yield db

    class Dispatcher:
        async def enqueue(self, *_args, **_kwargs):
            raise RuntimeError("本次浏览器回归未计划投递分析")

    app.dependency_overrides[get_session] = database
    app.dependency_overrides[get_dispatcher] = Dispatcher
    fixed = FixedModel()
    original = fixed.handle

    def handle(request):
        body = json.loads(request.content)
        if body.get("tools"):
            users = [m for m in body.get("messages", []) if m.get("role") == "user"]
            current = str(users[-1].get("content") or "") if users else ""
            fixed.sent = any(m.get("role") == "tool" for m in body.get("messages", [])[body.get("messages", []).index(users[-1]) + 1:]) if users else False
            fixed.binding = {"operation": "parse_clue_from_image" if "重新解析" in current else "revise_draft"}
        return original(request)

    fixed.handle = handle
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="error"))
    node = args.node or "node"
    try:
        with runtime_clients(cfg, live=False, ledger=None, fixed=fixed):
            task = asyncio.create_task(server.serve())
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.1)
            manifest_path = output / "fixture.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            env = {**os.environ, "CONTEXT_STACK_URL": f"http://127.0.0.1:{args.port}",
                   "CONTEXT_STACK_FIXTURE": str(manifest_path), "CONTEXT_SMOKE_OUTPUT": str(output)}
            process = await asyncio.create_subprocess_exec(node, str(ROOT / "scripts/smoke_context_stack.cjs"), env=env)
            code = await process.wait()
            async with factory() as db:
                final = await db.get(ChatSession, sid)
                manifest["final_context"] = final.context_state
            (output / "database.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            server.should_exit = True
            await task
            if chat_turns._RUNNING:
                await asyncio.gather(*list(chat_turns._RUNNING), return_exceptions=True)
            return code
    finally:
        server.should_exit = True
        await engine.dispose()


def main() -> int:
    from testcontainers.community.postgres import PostgresContainer
    from testcontainers.community.redis import RedisContainer

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="runs/_e7b6_20261019/browser_stack")
    parser.add_argument("--port", type=int, default=5199)
    parser.add_argument("--node")
    args = parser.parse_args()
    with PostgresContainer("pgvector/pgvector:0.8.6-pg16", driver="asyncpg") as postgres, RedisContainer("redis:7.4.11-alpine") as redis:
        return asyncio.run(execute(args, postgres.get_connection_url(),
            f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"))


if __name__ == "__main__":
    raise SystemExit(main())
