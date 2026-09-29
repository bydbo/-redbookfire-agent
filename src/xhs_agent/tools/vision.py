"""看图补描述：把素材关键帧交给多模态模型，转成标签和一句话描述。

没有多模态 Key 时返回 None，索引会退回到「文件名 + 人工说明」的标签体系。
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

from ..config import AppConfig
from ..util import extract_json

PROMPT = """你在帮一个小红书素材库做标注。看这几张来自同一条素材的截图，输出 JSON：
{
  "title": "12 字以内的素材标题",
  "tags": ["3-8 个标签，优先写画面里能直接看到的物体、场景、动作、人物身份、情绪氛围"],
  "description": "一句话描述画面里发生了什么，不要猜地名和品牌"
}
只输出 JSON。"""


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
        content = [{"type": "text", "text": PROMPT + (f"\n文件名提示：{hint}" if hint else "")}]
        for path in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": _data_url(path)},
            })
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
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