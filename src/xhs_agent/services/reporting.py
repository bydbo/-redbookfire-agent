"""报告用例：把一次运行的结果组装成报告 model 并渲染落盘。

用途：`build_report_model` 是纯函数，产出 `tools/report.py` 约定的 model 形状；
      `render_and_save` 渲染 Markdown 与单文件 HTML，经 `RunStore` 落盘到 `runs/<run_id>/`。
输入：热点条目、运行元信息、`RunStore`；历史预览仅消费服务端运行范围 URL。
输出：报告 model dict；`render_and_save` 返回 `(markdown 路径, html 路径)`。
"""

from __future__ import annotations

from typing import Any

from ..tools.report import render_html, render_markdown
from ..tools.trace import RunStore


def build_report_model(*, run_id: str, created_at: str, materials_count: int | None,
                       config: dict[str, Any], hotspots: list[dict[str, Any]],
                       totals: dict[str, Any], errors: list[str],
                       vector_coverage: dict[str, Any] | None = None) -> dict[str, Any]:
    """组装报告 model（字段契约见 `tools/report.py` 的模块 docstring）。

    `vector_coverage` 是可选项：给了才写进 `meta`，报告据此渲染"向量覆盖率"。
    """
    meta: dict[str, Any] = {
        "run_id": run_id,
        "created_at": created_at,
        "materials_count": materials_count,
    }
    if vector_coverage:
        meta["vector_coverage"] = dict(vector_coverage)
    return {
        "meta": meta,
        "config": dict(config),
        "hotspots": list(hotspots),
        "totals": dict(totals),
        "errors": list(errors),
    }


def hotspot_entry(clue: dict[str, Any], coverage: dict[str, Any],
                  candidates: list[dict[str, Any]], *,
                  gap_advice: list[dict[str, Any]] | None = None,
                  draft: dict[str, Any] | None = None,
                  error: str = "") -> dict[str, Any]:
    """一条热点结果（与 `tools/report.py` 的 `hotspots` 元素同形）。"""
    entry: dict[str, Any] = {
        "clue": dict(clue or {}),
        "coverage": dict(coverage or {}),
        "candidates": list(candidates or []),
        "draft": draft,
        "error": error,
    }
    if gap_advice:
        entry["gap_advice"] = list(gap_advice)
    return entry


def render_and_save(model: dict[str, Any], store: RunStore, *, base_dir: str) -> tuple[str, str]:
    """渲染 Markdown + 单文件 HTML 并落盘，返回两个产物路径。

    `base_dir` 保留调用兼容；历史预览通过已校验的运行范围 URL 提供，不暴露缓存路径。
    """
    markdown = render_markdown(model)
    html = render_html(model, base_dir=base_dir)
    return store.save_text("report.md", markdown), store.save_text("report.html", html)
