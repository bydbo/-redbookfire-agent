"""产物版本、图片缓存身份与观测事件的离线单测（S7.25 / S7.27 / S7.28）。

不连库：只覆盖确定性的键构造、身份失效条件、观测幂等键与清洗规则；真实存储路径见
`tests/integration/test_context_storage.py`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from xhs_agent.config import AppConfig, load_config
from xhs_agent.core import observations
from xhs_agent.services import artifact_versions, image_cache
from xhs_agent.services.chat import chat_root

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def write_config(tmp_path: Path, body: str = "") -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\nruns_dir = "{(tmp_path / "runs").as_posix()}"\n' + body,
        encoding="utf-8",
    )
    return load_config(str(path))


class TestArtifactKeys:
    def test_object_key_is_scoped_by_type_run_and_position(self) -> None:
        assert artifact_versions.object_key("draft", "r1", 2) == "draft:r1:2"
        assert artifact_versions.object_key("clue", "r1", 2) == "clue:r1:2"

    def test_request_key_changes_with_turn_and_call(self) -> None:
        base = {"object_key_value": "draft:r1:1", "request": "更短"}
        first = artifact_versions.request_key("s1", turn_id="t1", call_id="c1", **base)
        second = artifact_versions.request_key("s1", turn_id="t2", call_id="c1", **base)
        third = artifact_versions.request_key("s1", turn_id="t1", call_id="c2", **base)
        assert len({first, second, third}) == 3

    def test_request_key_changes_with_request_text(self) -> None:
        one = artifact_versions.request_key("s1", "draft:r1:1", turn_id="t", call_id="c",
                                            request="更短")
        two = artifact_versions.request_key("s1", "draft:r1:1", turn_id="t", call_id="c",
                                            request="更长")
        assert one != two

    def test_request_key_is_stable_for_same_inputs(self) -> None:
        args = {"turn_id": "t", "call_id": "c", "request": "更短"}
        assert artifact_versions.request_key("s1", "draft:r1:1", **args) == \
            artifact_versions.request_key("s1", "draft:r1:1", **args)


class TestImageView:
    def test_view_has_contract_fields(self) -> None:
        class Row:
            id = "00000000-0000-0000-0000-000000000001"
            session_id = "00000000-0000-0000-0000-000000000002"
            object_key = "draft:r:1"
            object_type = "draft"
            run_id = None
            hotspot_position = 1
            status = "succeeded"
            version_no = 2
            base_version_id = None
            content = {"revised": {"body": "x"}}
            source = {"turn_id": "t"}
            prompt_versions = {"copy_revise": 1}
            usage = {"cost_cny": 0.01}
            error = None
            created_at = None
            updated_at = None

        view = artifact_versions.view(Row())  # type: ignore[arg-type]
        assert view["object_key"] == "draft:r:1"
        assert view["version_no"] == 2
        assert view["prompt_versions"] == {"copy_revise": 1}
        assert view["run_id"] is None


class TestImageIdentity:
    def test_digest_is_content_based(self) -> None:
        assert image_cache.digest_bytes(PNG) == image_cache.digest_bytes(bytes(PNG))
        assert image_cache.digest_bytes(PNG) != image_cache.digest_bytes(PNG + b"x")

    def test_identity_is_stable_for_same_inputs(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        one = image_cache.build_identity(cfg, ["a" * 64])
        two = image_cache.build_identity(cfg, ["a" * 64])
        assert image_cache.cache_key(one) == image_cache.cache_key(two)

    def test_order_changes_the_key(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        straight = image_cache.build_identity(cfg, ["a" * 64, "b" * 64])
        reversed_order = image_cache.build_identity(cfg, ["b" * 64, "a" * 64])
        assert image_cache.cache_key(straight) != image_cache.cache_key(reversed_order)

    def test_model_change_invalidates(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        other = write_config(tmp_path, '[vision]\nmodel = "another-vl"\n')
        assert image_cache.cache_key(image_cache.build_identity(cfg, ["a" * 64])) != \
            image_cache.cache_key(image_cache.build_identity(other, ["a" * 64]))

    def test_multi_vs_single_prompt_changes_identity(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        single = image_cache.build_identity(cfg, ["a" * 64])
        multi = image_cache.build_identity(cfg, ["a" * 64], multi=True)
        assert single["prompt_digest"] != multi["prompt_digest"]

    def test_params_digest_is_present(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        identity = image_cache.build_identity(cfg, ["a" * 64])
        assert identity["params_digest"]
        assert identity["schema_version"] == image_cache.SCHEMA_VERSION


class TestEnsureDigests:
    @staticmethod
    def _write_chat_file(
        tmp_path: Path, cfg: AppConfig, name: str, data: bytes
    ) -> dict[str, Any]:
        """写一个真实附件文件（归属校验需要它真实存在于会话目录内）。"""
        from pathlib import Path

        target = Path(chat_root(cfg)) / "s1" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        # 存储路径与生产一致：runs_dir 不在工程根下时保存绝对路径（`_relative` 同口径）
        return {"path": str(target), "name": name,
                "bytes": len(data), "mtime_ns": target.stat().st_mtime_ns}

    @pytest.mark.asyncio
    async def test_fills_missing_digest_and_keeps_existing(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        seen: list[str] = []

        async def reader(path: str) -> bytes:
            seen.append(path)
            return PNG

        first = self._write_chat_file(tmp_path, cfg, "a.png", PNG)
        second = self._write_chat_file(tmp_path, cfg, "b.png", PNG)
        second["sha256"] = "precomputed"
        attachments = [first, second]
        digests, elapsed_ms, failures = await image_cache.ensure_digests(
            cfg, "s1", attachments, reader=reader
        )
        # 元数据未变化：有摘要的不重读；没摘要的补读一次（注入的 reader 收到该路径）
        assert seen == [first["path"]]
        assert digests == [image_cache.digest_bytes(PNG), "precomputed"]
        assert attachments[0]["sha256"] == image_cache.digest_bytes(PNG)
        assert elapsed_ms >= 0 and failures == []

    @pytest.mark.asyncio
    async def test_changed_content_recomputes_digest(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        seen: list[str] = []

        async def reader(path: str) -> bytes:
            seen.append(path)
            return PNG + b"changed"

        item = self._write_chat_file(tmp_path, cfg, "a.png", PNG)
        item["sha256"] = image_cache.digest_bytes(PNG)
        # 外部改写文件：mtime 变化 → 必须重读重算（E7CTX-10）
        target = Path(chat_root(cfg)) / "s1" / "a.png"
        target.write_bytes(PNG + b"changed")
        digests, _elapsed, failures = await image_cache.ensure_digests(
            cfg, "s1", [item], reader=reader
        )
        assert seen == [item["path"]]
        assert failures == []
        assert digests == [image_cache.digest_bytes(PNG + b"changed")]

    @pytest.mark.asyncio
    async def test_missing_file_is_a_failure(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        digests, _elapsed, failures = await image_cache.ensure_digests(
            cfg, "s1", [{"path": "runs/_chat/s1/missing.png", "name": "missing",
                         "sha256": "x"}]
        )
        assert digests == [""]
        assert failures == ["missing"]

    @pytest.mark.asyncio
    async def test_path_outside_session_is_a_failure(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        evil = tmp_path / "runs" / "_chat" / "s2" / "evil.png"
        evil.parent.mkdir(parents=True, exist_ok=True)
        evil.write_bytes(PNG)
        digests, _elapsed, failures = await image_cache.ensure_digests(
            cfg, "s1", [{"path": "runs/_chat/s2/evil.png", "name": "evil"}]
        )
        assert digests == [""]
        assert failures == ["evil"]


class TestObservations:
    def test_event_id_is_stable_and_distinct(self) -> None:
        one = observations.event_id("context_assembled", "s1", "t1:1")
        two = observations.event_id("context_assembled", "s1", "t1:1")
        three = observations.event_id("context_assembled", "s1", "t1:2")
        assert one == two and one != three

    def test_sanitize_drops_private_payload(self) -> None:
        cleaned = observations.sanitize(
            {
                "user_text": "用户原文",
                "args": {"path": "data/materials/x.mp4"},
                "nested": {"evidence": "原文", "count": 3},
                "long": "x" * 500,
                "flag": True,
            }
        )
        assert "user_text" not in cleaned
        assert "args" not in cleaned
        assert "evidence" not in cleaned["nested"]
        assert cleaned["nested"]["count"] == 3
        assert len(cleaned["long"]) <= observations.MAX_STRING_CHARS + 1
        assert cleaned["flag"] is True

    @pytest.mark.asyncio
    async def test_unknown_event_type_is_ignored(self) -> None:
        class DummySession:
            async def begin_nested(self) -> Any:  # pragma: no cover - 不会走到
                raise AssertionError("未知类型不应写库")

        written = await observations.record(
            DummySession(),  # type: ignore[arg-type]
            event_type="unknown", session_id="s1", key="k",
        )
        assert written is False
