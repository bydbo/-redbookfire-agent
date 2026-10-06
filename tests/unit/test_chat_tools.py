"""「跑完整分析」工具的离线单测（S7.3）：参数校验、工具 schema 与失败回话。

真链路（提交分析 → Celery → 轮询 → 汇总）在集成用例里跑；这里只测不碰数据库的分支。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from xhs_agent.config import AppConfig, load_config
from xhs_agent.services.chat_tools import (
    TOOL_RUN_ANALYSIS,
    TOOLS,
    ToolContext,
    ToolOutcome,
    run_hotspot_analysis,
)


def write_config(tmp_path: Path) -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n',
        encoding="utf-8",
    )
    return load_config(str(path))


def context(tmp_path: Path) -> ToolContext:
    """只有 cfg 是真的：这些用例在用到 session 之前就该返回。"""
    return ToolContext(cfg=write_config(tmp_path), session=cast(Any, None))


class TestToolSchema:
    def test_single_tool_with_documented_parameters(self) -> None:
        assert len(TOOLS) == 1
        function = TOOLS[0]["function"]
        assert TOOLS[0]["type"] == "function"
        assert function["name"] == TOOL_RUN_ANALYSIS
        properties = function["parameters"]["properties"]
        assert set(properties) == {"hotspot", "topk"}
        assert function["parameters"]["required"] == []      # 只发图片时热点为空
        assert "description" in function and len(function["description"]) > 20

    def test_tool_message_is_json(self) -> None:
        outcome = ToolOutcome(status="failed", payload={"run_id": "r-1"}, error="炸了")
        payload = json.loads(outcome.as_tool_message())
        assert payload == {"status": "failed", "run_id": "r-1", "error": "炸了"}


class TestArguments:
    @pytest.mark.asyncio
    async def test_too_long_hotspot_is_rejected(self, tmp_path: Path) -> None:
        outcome = await run_hotspot_analysis(context(tmp_path), {"hotspot": "热" * 501})
        assert outcome.status == "failed" and "500" in outcome.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("topk", [0, 21, "abc"])
    async def test_bad_topk_is_rejected(self, tmp_path: Path, topk: Any) -> None:
        outcome = await run_hotspot_analysis(context(tmp_path),
                                             {"hotspot": "夜跑", "topk": topk})
        assert outcome.status == "failed" and "topk" in outcome.error

    @pytest.mark.asyncio
    async def test_without_text_and_image_asks_for_input(self, tmp_path: Path) -> None:
        outcome = await run_hotspot_analysis(context(tmp_path), {})
        assert outcome.status == "failed"
        assert "问用户" in outcome.error or "想蹭哪条热点" in outcome.error

    @pytest.mark.asyncio
    async def test_unreadable_attachment_fails_politely(self, tmp_path: Path) -> None:
        ctx = context(tmp_path)
        outcome = await run_hotspot_analysis(
            ctx, {}, attachments=[{"path": "runs/_chat/没有这个文件.png", "kind": "image"}])
        assert outcome.status == "failed"
        assert "图片" in outcome.error
        assert outcome.steps and outcome.steps[0]["stage"] == "解析图片"
