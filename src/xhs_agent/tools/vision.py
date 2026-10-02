"""看图补描述：把素材关键帧交给多模态模型，转成标签和一句话描述。

用途：给没有人工说明的素材补标题 / 标签 / 描述，是素材索引里的一个可选步骤。
输入：`AppConfig`（读 `[vision]` 段与密钥）；返回的 `describe(frames, hint)` 收关键帧路径列表。
输出：`{"title": str, "tags": list[str], "description": str}`，或 None 表示本次不出结果。
prompt 来源：`src/xhs_agent/prompts/material_tagging.md`（五段结构，见
`docs/contracts/prompt契约.md`）——本模块**不再内联 prompt 文本**。
能力裁剪：开关关闭、没有密钥、机器没有 ffmpeg 时 `make_describer` 直接返回 None，
索引会退回「文件名 + 人工说明」的标签体系——这是能力裁剪，不是运行时降级（AGENTS.md 第 4 节）。
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

from ..config import AppConfig
from ..util import extract_json
from . import prompt as prompt_tools

TASK_ID = "material_tagging"


def make_describer(cfg: AppConfig):
    """返回一个 (frames, hint) -> dict|None 的函数；不可用时返回 None。"""
    if not cfg.vision.available():
        return None

    base_url = (cfg.vision.base_url or "").rstrip("/")
    api_key = cfg.vision.resolved_key()
    model = cfg.vision.model
    max_frames = max(1, cfg.vision.max_frames)

    def describe(frames: list, hint: str = "") -> dict | None:
        images = [f for f in frames[:max_frames] if os.path.exists(f)]
        if not images:
            return None
        # prompt 资产缺失或变量不全时直接抛 PromptError，不静默降级（契约 §四）
        rendered = prompt_tools.render(TASK_ID, file_name_hint=hint or "（未提供）")
        content: list = [{"type": "text", "text": rendered.user}]
        for path in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": _data_url(path)},
            })
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": rendered.system},
                {"role": "user", "content": content},
            ],
            "temperature": 0.2,
            "max_tokens": 600,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=cfg.llm.timeout_s) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
            text = data["choices"][0]["message"]["content"]
            parsed = extract_json(text)
        except (urllib.error.URLError, KeyError, IndexError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        tags = parsed.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.replace("，", ",").split(",")]
        return {
            "title": str(parsed.get("title") or "").strip(),
            "tags": [str(t).strip() for t in tags if str(t).strip()],
            "description": str(parsed.get("description") or "").strip(),
        }

    return describe


def _data_url(path: str) -> str:
    with open(path, "rb") as fh:
        payload = base64.b64encode(fh.read()).decode("ascii")
    return f"data:image/jpeg;base64,{payload}"