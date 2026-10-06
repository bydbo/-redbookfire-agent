"""对话存储的离线单测（S7.2）：纯函数口径 + `[chat]` 配置 + 附件落盘/防穿越。

真正的库往返（会话、消息、级联、running→中断）在 `tests/integration/test_chat_storage.py`。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from xhs_agent.config import AppConfig, ConfigError, load_config
from xhs_agent.core.errors import NotFoundError
from xhs_agent.services.chat import (
    DEFAULT_TITLE,
    TITLE_CHARS,
    attachment_target,
    chat_root,
    derive_title,
    resolve_attachment,
    save_attachment,
)


def write_config(tmp_path: Path, body: str = "") -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n' + body,
        encoding="utf-8",
    )
    return load_config(str(path))


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return write_config(tmp_path)


class TestDeriveTitle:
    def test_folds_whitespace_and_truncates(self) -> None:
        assert derive_title("  帮我\n蹭一下   这个热点  ") == "帮我 蹭一下 这个热点"
        assert len(derive_title("热" * 50)) == TITLE_CHARS

    @pytest.mark.parametrize("text", ["", "   ", "\n\n"])
    def test_blank_falls_back(self, text: str) -> None:
        assert derive_title(text) == DEFAULT_TITLE


class TestAttachmentPaths:
    def test_target_uses_session_dir_and_mime_suffix(self, cfg: AppConfig) -> None:
        session_id = uuid.uuid4()
        target = attachment_target(cfg, session_id, "image/webp")
        assert Path(target).parent == Path(chat_root(cfg), str(session_id))
        assert target.endswith(".webp")
        assert attachment_target(cfg, session_id, "image/jpeg").endswith(".jpg")
        assert attachment_target(cfg, session_id, "image/png").endswith(".png")
        assert attachment_target(cfg, session_id, "application/pdf").endswith(".bin")

    def test_save_then_resolve_round_trip(self, cfg: AppConfig) -> None:
        session_id = uuid.uuid4()
        saved = save_attachment(cfg, session_id, b"\x89PNG-data", name="热点.png",
                                mime="image/png")

        assert saved["kind"] == "image" and saved["bytes"] == 9
        assert saved["name"] == "热点.png"
        assert Path(saved["path"]).name.endswith(".png")
        resolved = resolve_attachment(cfg, session_id, saved["path"])
        assert Path(resolved).read_bytes() == b"\x89PNG-data"

    def test_resolve_rejects_other_session_and_traversal(self, cfg: AppConfig) -> None:
        session_id = uuid.uuid4()
        saved = save_attachment(cfg, session_id, b"x", name="a.png", mime="image/png")

        with pytest.raises(NotFoundError):
            resolve_attachment(cfg, uuid.uuid4(), saved["path"])
        with pytest.raises(NotFoundError):
            resolve_attachment(cfg, session_id, "../../config/config.toml")
        with pytest.raises(NotFoundError):
            resolve_attachment(cfg, session_id, saved["path"] + ".missing")


class TestChatConfig:
    def test_defaults(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        assert cfg.chat.max_tool_rounds == 3
        assert cfg.chat.attachment_max_mb == 10
        assert cfg.chat.history_max_messages == 20
        assert cfg.chat.turn_stale_seconds == 300

    def test_describe_exposes_chat_block(self, tmp_path: Path) -> None:
        described = write_config(tmp_path).describe()
        assert described["chat"]["max_tool_rounds"] == 3
        assert described["chat"]["attachment_max_mb"] == 10

    @pytest.mark.parametrize("body", [
        "[chat]\nmax_tool_rounds = 0\n",
        "[chat]\nmax_tool_rounds = 11\n",
        "[chat]\nattachment_max_mb = 0\n",
        "[chat]\nhistory_max_messages = 0\n",
        "[chat]\nturn_stale_seconds = 5\n",
    ])
    def test_invalid_values_are_rejected(self, tmp_path: Path, body: str) -> None:
        with pytest.raises(ConfigError):
            write_config(tmp_path, body)
