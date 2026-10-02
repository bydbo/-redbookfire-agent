"""单热点分析图的状态定义（S3.1）。

`total=False`：每个节点只返回自己写入的键，LangGraph 按键替换通道值。
多热点的累积（`report_model` / `prompt_versions`）由 `run_analysis` 在循环外维护。
"""

from __future__ import annotations

from typing import Any, TypedDict


class AnalysisState(TypedDict, total=False):
    """五节点共用的 state。"""

    hotspot_raw: str                      # 输入：热点原文
    clue: dict[str, Any]                  # 拆解节点产出（HotspotClue.to_dict()）
    retrieval: dict[str, Any]             # 检索节点产出：候选 / 覆盖度 / 向量覆盖率 / 通道计数
    gap_advice: list[dict[str, Any]]      # 缺口节点产出：结构化补拍建议
    draft: dict[str, Any] | None          # 撰稿节点产出（无候选时为 None）
    notes: list[str]                      # 节点留下的说明（如"没有候选、跳过撰稿"）
    prompt_versions: dict[str, int]       # 各任务实际使用的 prompt 版本（prompt 契约 §五）
    report_model: dict[str, Any]          # run 级报告 model（跨热点累积）
    report_paths: dict[str, str]          # 报告产物路径
    errors: list[str]                     # 运行级错误（由 run_analysis 维护）
