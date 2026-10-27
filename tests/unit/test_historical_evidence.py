"""E8 历史文字证据与预览边界：内容变更、缺字段、证据篡改、旧版本正文。"""
from __future__ import annotations

import copy
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from xhs_agent.services.chat_tools import _historical_evidence
from xhs_agent.services.run_snapshots import digest, evidence_meta
from xhs_agent.tools.historical_media import frame_content_records, read_frame
from xhs_agent.tools.report import render_html, render_markdown

MID = str(uuid.uuid4())
RID = str(uuid.uuid4())


def inputs():
    clue = {"hotspot_raw": "夜跑", "elements": [{"type": "topic", "value": "夜跑"}],
            "why_it_works": [], "match_keywords": []}
    material = {"id": MID, "path": "m.mp4", "type": "video", "title": "", "description": "",
                "tags": [], "elements": [], "source": "filename"}
    draft = {"material_id": MID, "titles": [], "body": "基础正文", "tags": [],
             "cover_text": "", "first_3s": "", "shot_list": [], "compliance_notes": []}
    item = {"clue": clue, "evidence": {"clue": evidence_meta(
        origin="runtime", source_ref="run:clue", completeness="complete", payload=clue)},
        "candidates": [{"material_id": MID, "material": material,
                        "evidence": {"candidate": evidence_meta(
                            origin="cutover_capture", source_ref="migration:material",
                            completeness="complete", payload=material)}}]}
    return item, draft


def test_empty_text_fields_are_valid_but_absent_fields_are_not():
    item, draft = inputs()
    evidence, missing = _historical_evidence(item=item, base_draft=draft, run_id=RID, position=1)
    assert evidence is not None and missing == []
    assert evidence["sources"]["material"]["origin"] == "cutover_capture"
    del item["candidates"][0]["material"]["description"]
    evidence, missing = _historical_evidence(item=item, base_draft=draft, run_id=RID, position=1)
    assert evidence is None and "material.description" in missing


def test_version_digest_tamper_is_refused_without_run_fallback():
    item, draft = inputs()
    evidence, _ = _historical_evidence(item=item, base_draft=draft, run_id=RID, position=1)
    evidence["material"]["title"] = "tampered"
    evidence, missing = _historical_evidence(item=item, base_draft=draft, run_id=RID, position=1,
        base_version=SimpleNamespace(evidence_snapshot=evidence))
    assert evidence is None and "evidence_snapshot" in missing


def test_selected_version_body_is_bound_as_next_base():
    item, draft = inputs()
    evidence, _ = _historical_evidence(item=item, base_draft=draft, run_id=RID, position=1)
    selected = {**draft, "body": "第二稿自身正文"}
    result, missing = _historical_evidence(item=item, base_draft=selected, run_id=RID, position=1,
        base_version=SimpleNamespace(evidence_snapshot=evidence))
    assert missing == [] and result["base_draft"]["body"] == "第二稿自身正文"
    assert result["digest"] == digest({key: result[key] for key in ("clue", "material", "base_draft")})


def test_alias_identity_does_not_prove_original_draft_text():
    item, draft = inputs()
    original = str(uuid.uuid4())
    material = item["candidates"][0]["material"]
    material.update(id=original, original_material_id=original, title="A original text")
    item["candidates"][0]["evidence"]["candidate"] = evidence_meta(
        origin="migration_capture", source_ref="e6:verified", completeness="complete", payload=material)
    # Draft originally referred to canonical B, while winning candidate text came from A.
    result, missing = _historical_evidence(item=item, base_draft=draft, run_id=RID, position=1)
    assert result is None and "draft_material_provenance" in missing


def test_frame_hash_tracks_bytes_and_rejects_outside_and_links(tmp_path: Path):
    root = tmp_path / "keyframes"
    root.mkdir()
    image = root / "frame.jpg"
    image.write_bytes(b"first")
    first = frame_content_records([str(image)], keyframes_root=str(root), project_root=str(tmp_path))
    image.write_bytes(b"second")
    second = frame_content_records([str(image)], keyframes_root=str(root), project_root=str(tmp_path))
    assert first[0]["sha256"] != second[0]["sha256"]
    with pytest.raises(ValueError, match="path_invalid"):
        read_frame(str(tmp_path / "private.jpg"), keyframes_root=str(root), project_root=str(tmp_path))


def test_symlink_parent_is_rejected_before_resolving(tmp_path: Path, monkeypatch):
    from xhs_agent.tools import historical_media
    root = tmp_path / "cache" / "keyframes"
    root.mkdir(parents=True)
    image = root / "frame.jpg"
    image.write_bytes(b"image")
    monkeypatch.setattr(historical_media, "is_link", lambda path: Path(path) == root.parent)
    with pytest.raises(ValueError, match="path_invalid"):
        read_frame(str(image), keyframes_root=str(root), project_root=str(tmp_path))


def test_reports_preserve_missing_raw_and_never_use_raw_frame_paths():
    item = {"position": 2, "hotspot_raw": "原热点", "clue": None, "status": "failed",
            "error": "撰稿失败", "evidence": {"clue": {"origin": "missing", "completeness": "missing"}},
            "candidates": [{"material": {"keyframes": ["private/frame.jpg"]},
                            "current_material": {"exists": False, "preview_available": False},
                            "preview_url": "/api/runs/" + RID + "/hotspots/2/candidates/1/keyframes/0"}]}
    model = {"hotspots": [item]}
    for report in (render_html(model), render_markdown(model)):
        assert "原热点" in report and "历史线索缺失" in report and "原素材已删除" in report
        assert "private/frame.jpg" not in report
        assert "线索要素都有素材覆盖" not in report
    current = copy.deepcopy(model)
    candidate = current["hotspots"][0]["candidates"][0]
    candidate["current_material"] = {"exists": True, "preview_available": True}
    assert 'src="/api/runs/' in render_html(current)
    candidate["preview_url"] = "https://bad.example/image.jpg"
    assert "bad.example" not in render_html(current)


@pytest.mark.parametrize("status,integrity,expected", [
    ("succeeded", {"completeness": "complete", "origin": "runtime"}, "没有达到阈值的素材"),
    ("succeeded", {"completeness": "partial", "origin": "legacy_run_record"}, "旧候选完整性无法确认"),
    ("failed", None, "本次检索未完成或失败"),
])
def test_reports_distinguish_empty_history_and_failed_retrieval(status, integrity, expected):
    evidence = {"clue": {"origin": "runtime", "completeness": "complete"}}
    if integrity:
        evidence["candidate_integrity"] = integrity
    model = {"hotspots": [{"status": status, "evidence": evidence, "candidates": []}]}
    for result in (render_html(model), render_markdown(model)):
        assert expected in result
        if status == "failed":
            assert "旧候选完整性无法确认" not in result
