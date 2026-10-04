"""单元测试的公共夹具。

两条硬规则：
1. 配置相关用例绝不能读到开发者本机的真实 `config/.env` 或环境变量，否则在别人机器上必红；
2. 工具相关用例必须能在没有 ffmpeg、没有网络、没有真实素材的机器上跑通——依赖探测一律可注入。
"""

from __future__ import annotations

import struct

import pytest

from xhs_agent.schemas import HotspotClue, Material
from xhs_agent.tools import media

CONTRACT_ENV_NAMES = (
    "XHS_CONFIG_PATH",
    "XHS_LOG_LEVEL",
    "XHS_LOG_FORMAT",
    "XHS_FRONTEND_SERVE",
    "XHS_LLM_PROVIDER",
    "XHS_LLM_BASE_URL",
    "XHS_LLM_MODEL",
    "XHS_LLM_API_KEY",
    "XHS_EMBEDDING_MODEL",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_HOST",
)


@pytest.fixture(autouse=True)
def clean_contract_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """清空契约里出现过的环境变量，返回 monkeypatch 供用例注入。"""
    for name in CONTRACT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _minimal_png(width: int, height: int) -> bytes:
    """最小 PNG：签名 + IHDR 头，够 `materials._image_size` 读出宽高（不需要真图片）。"""
    body = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
    body += struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return body + b"\x00" * 8


@pytest.fixture
def make_png():
    """返回一个 (width, height) -> bytes 的 PNG 构造器。"""
    return _minimal_png


@pytest.fixture
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """把 ffmpeg / ffprobe 探测固定为「没装」：用例结果不依赖开发机装没装。"""
    monkeypatch.setattr(media, "have_ffmpeg", lambda: False)
    monkeypatch.setattr(media, "have_ffprobe", lambda: False)
    return monkeypatch


@pytest.fixture
def make_material():
    """构造素材对象。默认 `mtime=0`（不触发时效加减分），需要时用关键字覆盖任意字段。"""

    def _make(material_id: str = "m_1", path: str = "D:/materials/clip.mp4",
              **overrides) -> Material:
        base = {
            "id": material_id,
            "path": path,
            "title": "",
            "description": "",
            "tags": [],
            "mtime": 0.0,
            "indexed_at": "2026-10-01T00:00:00+08:00",
        }
        base.update(overrides)
        return Material(**base)

    return _make


@pytest.fixture
def make_clue():
    """构造热点线索。`elements` 用 `[(type, value), ...]` 简写，权重与置信度走默认值。"""

    def _make(raw: str = "某顶流明星打羽毛球被拍", elements=None, **overrides) -> HotspotClue:
        data = {
            "hotspot_raw": raw,
            "elements": [{"type": etype, "value": value}
                         for etype, value in (elements or [("topic", "羽毛球")])],
        }
        data.update(overrides)
        return HotspotClue.from_dict(data)

    return _make


@pytest.fixture
def materials_dir(tmp_path, make_png):
    """假素材库：真 mp4（内容随便）+ 旁车说明 + 最小 PNG + 一个隐藏文件。"""
    root = tmp_path / "materials"
    root.mkdir()
    (root / "球场-挥拍.mp4").write_bytes(b"\x00" * 64)
    (root / "球场-挥拍.mp4.txt").write_text("标题: 球场热身\n标签: 羽毛球, 挥拍\n",
                                            encoding="utf-8")
    (root / "宠物猫.png").write_bytes(make_png(1080, 1920))
    (root / ".hidden.mp4").write_bytes(b"\x00" * 8)
    return root


@pytest.fixture
def sample_report_model():
    """报告渲染的固定输入；字段契约见 `src/xhs_agent/tools/report.py` 的模块 docstring。"""
    return {
        "meta": {"run_id": "run-1", "created_at": "2026-10-01T10:00:00+08:00",
                 "materials_count": 2},
        "config": {"llm": {"provider": "openai_compatible", "model": "deepseek-flash"},
                   "materials_dir": "D:/materials"},
        "hotspots": [{
            "clue": {
                "hotspot_raw": "某顶流明星打羽毛球被拍",
                "why_it_works": ["反差：身份反差"],
                "mechanisms": [{"name": "反差", "explain": "身份反差"}],
                "elements": [{"type": "topic", "value": "羽毛球", "weight": 0.9,
                              "confidence": 0.9, "evidence": "羽毛球"}],
                "borrow_angles": ["同款球场热场"],
            },
            "coverage": {
                "ratio": 0.5,
                "covered": [{"type": "topic", "value": "羽毛球", "weight": 0.9,
                             "confidence": 0.9, "evidence": ""}],
                "gaps": [{"type": "ip", "value": "顶流明星", "weight": 0.9,
                          "confidence": 0.9, "evidence": ""}],
            },
            "candidates": [{
                "material_id": "m_1",
                "score": 0.8,
                "rank": 1,
                "reasons": ["命中主题「羽毛球」"],
                "usage": "放开头 3 秒",
                "hits": [{"element_type": "topic", "clue_value": "羽毛球",
                          "hit_value": "羽毛球", "similarity": 0.9, "contribution": 0.8}],
                "missing": [],
                "material": {"id": "m_1", "path": "D:/materials/球场.mp4", "type": "video",
                             "title": "球场热身", "tags": ["羽毛球"]},
            }],
            "draft": {"titles": [{"text": "标题", "style": "直给"}], "body": "正文",
                      "tags": ["#羽毛球"], "cover_text": "封面", "first_3s": "开头",
                      "shot_list": ["先拍球场"], "compliance_notes": ["别用明星肖像"]},
        }],
        "totals": {"llm_calls": 2, "prompt_tokens": 100, "completion_tokens": 50,
                   "cost_cny": 0.0123, "latency_ms": 1200},
        "errors": [],
    }
