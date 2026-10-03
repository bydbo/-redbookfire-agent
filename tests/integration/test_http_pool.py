"""连接池生效的可观测证据（S3.5）。

用法：进程内起一个极小 ASGI 应用（uvicorn，loopback），记录每次请求的 `scope["client"]`
      端口；让 `EmbeddingClient` 用**同一个** httpx 客户端发 16 条文本（批次上限 10 →
      两次请求），服务端应当只见到 1 个连接——这就是 keep-alive 连接复用的直接证据。
为什么放集成层：单元用例的约定是"不联网"，这里要真的绑定 loopback 端口。
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import uvicorn

from xhs_agent.config import EmbeddingConfig, EnvView
from xhs_agent.tools.embedding import EmbeddingClient
from xhs_agent.tools.http import build_http_client

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DIM = 1024


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def embedding_cfg(base_url: str, *, batch_size: int) -> EmbeddingConfig:
    """指向进程内假端点的向量配置；带一个假密钥（httpx 会校验请求头）。"""
    cfg = EmbeddingConfig(base_url=base_url, model="text-embedding-v3", dim=DIM,
                          batch_size=batch_size, timeout_s=5, max_retries=0)
    cfg._env = EnvView({}, {cfg.api_key_env: "sk-test"})
    return cfg


class CountingApp:
    """极小 ASGI 应用：按请求条数返回固定向量，并记录来源端口与请求数。"""

    def __init__(self) -> None:
        self.ports: list[int] = []
        self.requests = 0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        self.requests += 1
        self.ports.append(int(scope["client"][1]))
        payload = await self._read_json(receive)
        count = len(payload.get("input") or [])
        body = json.dumps({
            "data": [{"index": i, "embedding": [0.1] * DIM} for i in range(count)],
            "usage": {"prompt_tokens": 1},
        }).encode("utf-8")
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _read_json(receive: Any) -> dict[str, Any]:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        raw = b"".join(chunks) or b"{}"
        parsed: dict[str, Any] = json.loads(raw.decode("utf-8"))
        return parsed


@pytest_asyncio.fixture
async def counting_server() -> AsyncIterator[tuple[str, CountingApp]]:
    """起一个进程内 uvicorn，产出 (base_url, app)；用例结束关服。"""
    app = CountingApp()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    task = asyncio.create_task(server.serve())
    for _ in range(500):        # 等 uvicorn 真正监听（最多 ~5 秒）
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started, "进程内 uvicorn 没能在 5 秒内就绪"
    try:
        yield f"http://127.0.0.1:{port}/v1", app
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


class TestConnectionReuse:
    async def test_two_batches_reuse_one_connection(
            self, counting_server: tuple[str, CountingApp]) -> None:
        base_url, app = counting_server
        cfg = embedding_cfg(base_url, batch_size=16)

        async with build_http_client(5) as http:
            result = await EmbeddingClient(cfg, client=http).embed(
                [f"t{i}" for i in range(16)])

        assert len(result.vectors) == 16
        assert app.requests == 2          # 批次上限 10 → 10 + 6
        assert len(set(app.ports)) == 1   # 两次请求复用同一条 TCP 连接 → 连接池生效

    async def test_separate_clients_open_separate_connections(
            self, counting_server: tuple[str, CountingApp]) -> None:
        """反证：每次新建客户端就会各开一条连接（说明上一条用例不是因为服务端合并了连接）。"""
        base_url, app = counting_server
        cfg = embedding_cfg(base_url, batch_size=10)

        for _ in range(2):
            async with build_http_client(5) as http:
                await EmbeddingClient(cfg, client=http).embed(["a"])

        assert app.requests == 2
        assert len(set(app.ports)) == 2   # 两个客户端 → 两条连接
