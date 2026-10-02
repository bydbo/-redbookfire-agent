"""候选解释：线索 + 候选素材 → 覆盖后的候选（`material_select`）。

输入：线索 + S2.6 产出的终态候选（已带规则解释）+ `StructuredCaller`。
输出：`AgentOutcome(value=[MatchCandidate, ...], version=prompt 版本)`；没有候选时不调模型、
      `version=None`（未参与本次运行的任务不写进 `prompt_versions`）。
不做降级：模型返回的条目与候选对不上、缺 `reasons` / `usage`，一律抛错。
"""

from __future__ import annotations

import json
from typing import Any

from ..schemas import HotspotClue, MatchCandidate
from ..tools import prompt as prompt_tool
from ..tools.llm import LLMError, StructuredCaller
from .base import AgentOutcome

TASK_ID = "material_select"

# 容忍模型把候选数组包一层的几种常见键名（05 Output Schema 只约定条目字段）
_LIST_KEYS = ("candidates", "matches", "results", "items", "explanations")


def explain_candidates(clue: HotspotClue, candidates: list[MatchCandidate],
                       caller: StructuredCaller) -> AgentOutcome:
    """为整批候选补上模型版 `reasons` / `usage`（覆盖检索层的规则解释）。"""
    if not candidates:
        return AgentOutcome(value=[], task_id=TASK_ID, version=None)

    rendered = prompt_tool.render(
        TASK_ID,
        hotspot_raw=clue.hotspot_raw,
        clue_json=json.dumps(clue.to_dict(), ensure_ascii=False),
        candidates_json=json.dumps([_candidate_payload(item) for item in candidates],
                                   ensure_ascii=False),
    )
    explanations, _result = caller.call(
        TASK_ID, rendered.system, rendered.user,
        parse=lambda payload: _parse_explanations(payload, candidates),
    )
    updated = [candidate.model_copy(update={"reasons": item["reasons"], "usage": item["usage"]})
               for candidate, item in zip(candidates, explanations, strict=True)]
    for candidate in updated:
        candidate.finalize()  # 契约硬约束：不允许出现无理由候选
    return AgentOutcome(value=updated, task_id=TASK_ID, version=rendered.version)


def _candidate_payload(candidate: MatchCandidate) -> dict:
    """交给模型的候选摘要：只给判断需要的字段，不把整条素材塞进去（控制 token）。"""
    material = candidate.material
    return {
        "material_id": candidate.material_id,
        "rank": candidate.rank,
        "score": round(candidate.score, 4),
        "recall_sources": list(candidate.recall_sources),
        "hits": [hit.to_dict() for hit in candidate.hits],
        "material": {
            "path": material.path,
            "type": material.type,
            "title": material.title,
            "description": material.description,
            "tags": list(material.tags),
        },
    }


def _as_str_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    items = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in items if str(item or "").strip()]


def _parse_explanations(payload: Any, candidates: list[MatchCandidate]) -> list[dict]:
    """折成「与候选一一对应、每条都有非空 reasons 与 usage」的列表。"""
    if isinstance(payload, dict) and "material_id" in payload:
        items = [payload]
    elif isinstance(payload, dict):
        items = next((payload[key] for key in _LIST_KEYS if isinstance(payload.get(key), list)),
                     None)
    else:
        items = payload
    if not isinstance(items, list):
        raise LLMError("material_select 返回结构异常：期望候选数组，或含候选数组的对象")

    by_id: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        material_id = str(item.get("material_id") or "").strip()
        if material_id:
            by_id[material_id] = item

    expected = [candidate.material_id for candidate in candidates]
    missing = [material_id for material_id in expected if material_id not in by_id]
    if missing:
        raise LLMError(f"material_select 缺少这些候选的解释：{missing}")
    unknown = [material_id for material_id in by_id if material_id not in set(expected)]
    if unknown:
        raise LLMError(f"material_select 返回了候选清单外的素材：{unknown}")

    explanations = []
    for material_id in expected:
        item = by_id[material_id]
        reasons = _as_str_list(item.get("reasons"))[:5]
        usage = str(item.get("usage") or "").strip()
        if not reasons:
            raise LLMError(f"material_select 的候选 {material_id} 缺少 reasons")
        if not usage:
            raise LLMError(f"material_select 的候选 {material_id} 缺少 usage")
        explanations.append({"reasons": reasons, "usage": usage})
    return explanations
