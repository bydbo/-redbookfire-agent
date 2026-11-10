"""S7.30 会话重命名：请求体校验契约（不依赖数据库 / Docker）。

覆盖 trim、空值、Unicode 字符口径、额外字段禁止；数据库侧的 `updated_at` 不变与
自动补名不覆盖由 `tests/integration/test_chat_api.py` 的 `integration` 用例验证。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from xhs_agent.api.models import ChatSessionRename
from xhs_agent.services.chat import rename_session


def test_trim_and_keep_inner_spaces() -> None:
    assert ChatSessionRename(title="  改名  测试  ").title == "改名  测试"


@pytest.mark.parametrize("raw", ["", "   ", "\t\n "])
def test_blank_title_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        ChatSessionRename(title=raw)


def test_length_counts_unicode_code_points_not_utf16_units() -> None:
    # Python len() 按 code point 计；100 个 emoji 合法，101 个拒绝（前端 UTF-16 会算成 200/202，
    # 但契约以后端 Unicode 字符数为准）。
    assert len(ChatSessionRename(title="😀" * 100).title) == 100
    with pytest.raises(ValidationError):
        ChatSessionRename(title="😀" * 101)


def test_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        ChatSessionRename.model_validate({"title": "新名", "updated_at": "2026-01-01"})


def test_title_is_required() -> None:
    with pytest.raises(ValidationError):
        ChatSessionRename.model_validate({})


@pytest.mark.asyncio
async def test_rename_session_writes_only_title_and_keeps_updated_at() -> None:
    """服务层：只写 title，updated_at 不变；不依赖 Docker 的等价回归。"""
    row = SimpleNamespace(
        id=uuid.uuid4(),
        title="旧标题",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: row)

    view = await rename_session(session, row.id, "新标题")

    assert view is not None
    assert view["title"] == "新标题"
    assert view["updated_at"] == row.updated_at.isoformat()
    assert row.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_rename_session_missing_returns_none_without_commit() -> None:
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)

    assert await rename_session(session, uuid.uuid4(), "新名") is None
    session.commit.assert_not_awaited()
