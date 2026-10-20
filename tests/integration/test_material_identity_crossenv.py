"""同一素材与数据库跨宿主机 → Linux 容器 → 宿主机同步，无真实模型。"""
from __future__ import annotations

import asyncio
import json
import os
import struct
from dataclasses import asdict
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

import docker
from alembic import command
from xhs_agent.config import load_config
from xhs_agent.services import materials as service
from xhs_agent.services.materials import sync_materials
from xhs_agent.tools import materials as material_tools

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[2]


async def test_host_container_host_preserves_identity_and_skips_vision(
        db_engine, postgres_dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    await asyncio.to_thread(command.upgrade, Config(str(ROOT / "alembic.ini")), "head")
    materials = tmp_path / "materials"
    materials.mkdir()
    png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
    (materials / "sample.png").write_bytes(
        png + struct.pack(">II", 32, 32) + b"\x08\x06\x00\x00\x00" + b"\x00" * 8)
    config_path = tmp_path / "host.toml"
    config_path.write_text(f'[paths]\nmaterials_dir="{materials.as_posix()}"\n'
        f'index_dir="{(tmp_path / "index").as_posix()}"\n', encoding="utf-8")
    cfg = load_config(str(config_path))
    monkeypatch.setattr(service, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(material_tools.media, "extract_keyframes", lambda path, *args, **kwargs: [path])
    calls = []

    def vision(_frames, hint):
        calls.append(hint)
        return {"title": "fixed", "description": "fixed image", "tags": ["fixed"]}

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        first = await sync_materials(session, cfg, vision=vision)
        assert first.added == 1 and len(calls) == 1
        await session.execute(text("UPDATE materials SET embedding=CAST(:v AS vector), "
            "embedding_model='fixed'"), {"v": "[1," + ",".join(["0"] * 1023) + "]"})
        await session.commit()
        before = (await session.execute(text("SELECT to_jsonb(m)::text FROM materials m"))).scalar_one()

    # 用实际应用镜像及当前源代码；不传真实 config/.env 或模型凭据。
    script = tmp_path / "container_sync.py"
    script.write_text('''import asyncio, json, os
from dataclasses import asdict
from xhs_agent.config import load_config
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from xhs_agent.services.materials import sync_materials
from xhs_agent.services import materials as service
from xhs_agent.tools import materials as material_tools
async def main():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    service.PROJECT_ROOT = "/fixture"
    material_tools.media.extract_keyframes = lambda path, *args, **kwargs: [path]
    calls = []
    def no_vision(*args, **kwargs):
        calls.append(True)
        return {"title": "unexpected"}
    async with async_sessionmaker(engine)() as session:
        report = await sync_materials(session, load_config("/fixture/linux.toml"), vision=no_vision)
        assert not calls, "unchanged material invoked vision"
        print(json.dumps(asdict(report)))
    await engine.dispose()
asyncio.run(main())
''', encoding="utf-8")
    (tmp_path / "linux.toml").write_text(
        '[paths]\nmaterials_dir="/fixture/materials"\nindex_dir="/tmp/index"\n', encoding="utf-8")
    url = make_url(postgres_dsn).set(host="host.docker.internal")
    client = docker.from_env()
    try:
        output = await asyncio.to_thread(client.containers.run,
            os.environ.get("E6_TEST_IMAGE", "xhs-agent:ci"),
            ["python", "/fixture/container_sync.py"], remove=True,
            volumes={str(tmp_path): {"bind": "/fixture", "mode": "ro"},
                     str(ROOT / "src"): {"bind": "/app/src", "mode": "ro"}},
            environment={"DATABASE_URL": url.render_as_string(hide_password=False),
                         "PYTHONPATH": "/app/src", "LANGFUSE_ENABLED": "false"},
            user=str(os.getuid()) if hasattr(os, "getuid") else None,
            extra_hosts={"host.docker.internal": "host-gateway"})
    finally:
        client.close()
    second = json.loads(output.decode().strip().splitlines()[-1])
    assert (second["added"], second["updated"], second["deleted"], second["unchanged"]) == (0, 0, 0, 1)
    async with factory() as session:
        third = await sync_materials(session, cfg, vision=vision)
        after = (await session.execute(text("SELECT to_jsonb(m)::text FROM materials m"))).scalar_one()
    assert asdict(third) == second and before == after and len(calls) == 1


async def test_linux_symlinks_fail_before_mutation(tmp_path):
    script = tmp_path / "linux_links.py"
    script.write_text('''import json, tempfile
from pathlib import Path
from xhs_agent.config import load_config
from xhs_agent.core.errors import BadRequestError
from xhs_agent.services.materials import companion_paths, list_trash, purge_trash, restore_from_trash
from xhs_agent.tools.material_paths import resolve_material
with tempfile.TemporaryDirectory() as directory:
    base = Path(directory)
    root = base / "materials"
    root.mkdir()
    outside = base / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.mp4"
    sentinel.write_bytes(b"untouched")
    config = base / "config.toml"
    config.write_text('[paths]\\nmaterials_dir="' + str(root) + '"\\n')
    cfg = load_config(str(config))
    (root / "_trash").symlink_to(outside, target_is_directory=True)
    checks = 0
    for operation in (lambda: list_trash(cfg), lambda: purge_trash(cfg, all_files=True),
                      lambda: restore_from_trash(cfg, "sentinel.mp4"),
                      lambda: resolve_material(str(root), "_trash/sentinel.mp4")):
        try:
            operation()
        except (ValueError, BadRequestError):
            checks += 1
        else:
            raise AssertionError("linked root accepted")
    assert sentinel.read_bytes() == b"untouched" and len(list(outside.iterdir())) == 1
    media = root / "sample.mp4"
    media.write_bytes(b"media")
    for destination in (outside / "sentinel.mp4", outside / "missing"):
        link = Path(str(media) + ".txt")
        link.symlink_to(destination)
        try:
            companion_paths(str(media))
        except BadRequestError:
            checks += 1
        else:
            raise AssertionError("sidecar link accepted")
        link.unlink()
    (root / "_trash").unlink()
    (root / "_trash").mkdir()
    (root / "_trash" / "nested").symlink_to(outside, target_is_directory=True)
    try:
        purge_trash(cfg, all_files=True)
    except BadRequestError:
        checks += 1
    else:
        raise AssertionError("nested link accepted")
    assert sentinel.read_bytes() == b"untouched" and media.read_bytes() == b"media"
    print(json.dumps({"checks": checks, "outside_unchanged": True}))
''', encoding="utf-8")
    client = docker.from_env()
    try:
        output = await asyncio.to_thread(client.containers.run,
            os.environ.get("E6_TEST_IMAGE", "xhs-agent:ci"),
            ["python", "/fixture/linux_links.py"], remove=True,
            volumes={str(tmp_path): {"bind": "/fixture", "mode": "ro"},
                     str(ROOT / "src"): {"bind": "/app/src", "mode": "ro"}},
            environment={"PYTHONPATH": "/app/src"},
            user=str(os.getuid()) if hasattr(os, "getuid") else None)
    finally:
        client.close()
    assert json.loads(output.decode().strip().splitlines()[-1]) == {
        "checks": 7, "outside_unchanged": True}
