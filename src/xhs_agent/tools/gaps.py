"""覆盖缺口 → 补拍建议（确定性规则表）。

用途：把 `Coverage.gaps` 里的缺口要素翻成可执行的补拍建议；一个 tool 只做这一件事。
输入：`Coverage`（或它的 `to_dict()` 结果）。
输出：`[{"type", "value", "label", "advice"}]`，顺序与 `gaps` 一致；无缺口时返回空列表。
输出稳定性：纯查表 + 固定兜底文案，不调模型、不看时钟、不读文件。
"""

from __future__ import annotations

from ..schemas import TYPE_LABELS, Coverage, Element

# 按要素类型给具体建议（口径见 docs/产品方案.md §7.5 与 §九 合规边界）
ADVICE_BY_TYPE: dict[str, str] = {
    "ip": "人物 IP 不直用：改拍「同款动作 / 平替场景」，不要出现他人肖像与姓名",
    "topic": "补拍主题相关素材，或去素材库再捞一轮",
    "scene": "补拍同场景环境镜头（空镜 / 全景）",
    "visual": "补拍对应画面：特写、慢动作或过程镜头",
    "emotion": "补拍情绪反应（表情变化、下意识动作）",
    "sound": "补拍或替换对应声音素材；音频只用原创或已授权来源",
    "conflict": "补拍反差结构：前后对比、预期与结果的对撞",
    "format": "按该形式补拍：同款流程 / 教程步骤 / 跟练动作",
    "audience": "补拍能体现目标人群的画面（同类人、同类场景）",
}

DEFAULT_ADVICE = "补拍相关素材，或去素材库再捞一轮"


def gap_advice(coverage) -> list[dict]:
    """缺口要素 → 补拍建议。

    输入：`coverage`（`Coverage` 或 `to_dict()` 结果）。
    输出：`[{"type": str, "value": str, "label": str, "advice": str}]`。
    """
    return [
        {
            "type": element.type,
            "value": element.value,
            "label": TYPE_LABELS.get(element.type, element.type),
            "advice": ADVICE_BY_TYPE.get(element.type, DEFAULT_ADVICE),
        }
        for element in _gaps(coverage)
    ]


def _gaps(coverage) -> list[Element]:
    if isinstance(coverage, Coverage):
        return list(coverage.gaps)
    if isinstance(coverage, dict):
        return [Element.from_dict(item) for item in (coverage.get("gaps") or [])]
    return []
