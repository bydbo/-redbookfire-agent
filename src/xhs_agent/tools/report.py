"""把一次运行的结果渲染成人能看的报告（Markdown + 单文件 HTML）。

用途：纯函数渲染，不读文件、不联网、不看系统时钟（生成时间来自输入）。
输入：`model` dict，形状如下（**任何键缺失都回退为空值，不抛异常**）：

    meta     : {"run_id": str, "created_at": str, "materials_count": int,
                "vector_coverage": {"enabled": bool, "model": str, "total": int,
                                    "with_embedding": int, "ratio": float}}  # 可选键
    config   : {"llm": {"provider": str, "model": str}, "materials_dir": str}
    hotspots : [{"clue": HotspotClue.to_dict(), "coverage": Coverage.to_dict(),
                 "candidates": [MatchCandidate.to_dict(), ...],
                 "draft": Draft.to_dict() | None, "error": str}]
    totals   : {"llm_calls": int, "prompt_tokens": int, "completion_tokens": int,
                "cost_cny": float, "latency_ms": int}
    errors   : [str, ...]

    区块口径见 `docs/产品方案.md` §8.1（机会总览 / 为什么能火 / 热点线索 / 可蹭角度 /
    素材匹配榜 / 覆盖缺口 / 初步文案 / 本次运行）。
输出：Markdown 字符串 / 单文件 HTML 字符串。同一输入必须得到同一输出。
"""

from __future__ import annotations

import html
import os

from ..schemas import TYPE_LABELS
from .historical_media import safe_preview_url


def _raw(item: dict) -> str:
    return str(item.get("hotspot_raw") or (item.get("clue") or {}).get("hotspot_raw") or "原文缺失")


def _evidence_notes(item: dict) -> list[str]:
    origins = {"runtime": "运行时保存", "legacy_run_record": "原运行记录",
               "migration_capture": "E6 迁移归档", "cutover_capture": "切换时采集",
               "missing": "缺失"}
    labels = {"clue": "线索", "candidate": "素材文字", "coverage": "覆盖度",
              "draft": "初稿", "gap_advice": "缺口建议", "retrieval": "检索信息",
              "execution": "执行配置", "candidate_integrity": "候选完整性"}
    notes = []
    for key, meta in (item.get("evidence") or {}).items():
        if not isinstance(meta, dict):
            continue
        label = labels.get(key, key)
        origin = origins.get(meta.get("origin"), "来源未知")
        completeness = {"complete": "完整", "partial": "部分缺失", "missing": "缺失"}.get(
            meta.get("completeness"), "完整度未知")
        notes.append(f"{label}：{origin}（{completeness}）")
    if "clue" in item and item.get("clue") is None:
        notes.append("历史线索缺失，无法确认原运行依据")
    if item.get("error"):
        notes.append(f"热点执行失败：{item['error']}")
    return notes


def _preview_note(candidate: dict) -> str:
    current = candidate.get("current_material") or {}
    if current.get("exists") is False:
        return "原素材已删除，历史文字依据仍保留"
    reason = current.get("reason")
    notes = {"material_version_changed": "素材版本已变化，历史预览无法确认",
             "preview_unverified": "历史预览缺少内容摘要，无法确认",
             "keyframe_content_changed": "关键帧内容已变化，历史预览无法确认",
             "keyframe_path_invalid": "历史关键帧路径不可用",
             "keyframes_missing": "历史关键帧缺失"}
    if reason in notes:
        return notes[reason]
    if current.get("file_available") is False:
        return "原媒体文件缺失；关键帧可用性单独校验"
    return "" if current.get("preview_available") else "历史预览不可用"


def _coverage_text(item: dict) -> str:
    coverage = item.get("coverage") or {}
    evidence = (item.get("evidence") or {}).get("coverage") or {}
    if "ratio" not in coverage or evidence.get("completeness") in {"missing", "partial"}:
        return "未知"
    return _fmt_score(coverage["ratio"])


def _empty_candidates_note(item: dict) -> str:
    integrity = (item.get("evidence") or {}).get("candidate_integrity")
    if integrity and integrity.get("completeness") == "complete":
        return "没有达到阈值的素材"
    if integrity:
        return "旧候选完整性无法确认"
    if item.get("status") == "failed":
        return "本次检索未完成或失败，尚无可确认候选"
    if item.get("status") in {"queued", "running"}:
        return "本次检索尚未完成"
    return "旧候选完整性无法确认" if item.get("evidence") else "没有达到阈值的素材"


def _fmt_score(score: float) -> str:
    return f"{score * 100:.0f}%"


def _model_label(config: dict) -> str:
    llm = config.get("llm") or {}
    return f"{llm.get('provider') or '未知'} / {llm.get('model') or '未知'}"


def _materials_count(meta: dict) -> str:
    value = meta.get("materials_count")
    return "未知" if value is None else str(value)


def _vector_coverage_text(vector: dict) -> str:
    """向量覆盖率文案（Markdown 与 HTML 共用）：`有向量/总数（比例）`，未启用时注明。"""
    total = vector.get("total", 0)
    with_embedding = vector.get("with_embedding", 0)
    note = "" if vector.get("enabled", True) else "（向量召回未启用）"
    return f"向量覆盖率 {with_embedding}/{total}（{_fmt_score(vector.get('ratio', 0))}）{note}"


def render_markdown(model: dict) -> str:
    meta = model.get("meta") or {}
    cfg = model.get("config") or {}
    hotspots = model.get("hotspots") or []
    totals = model.get("totals") or {}

    lines = [
        f"# 热点相关性报告｜{meta.get('run_id', '')}",
        "",
        f"- 生成时间：{meta.get('created_at', '')}",
        f"- 分析模型：{_model_label(cfg)}",
        f"- 素材库：`{cfg.get('materials_dir') or '未知'}`（{_materials_count(meta)} 条）",
        f"- 输入热点：{len(hotspots)} 条",
        "",
        "## 一、机会总览",
        "",
        "| # | 热点 | 覆盖度 | 最高分 | 首选素材 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for index, item in enumerate(hotspots, start=1):
        clue = item.get("clue") or {}
        coverage = item.get("coverage") or {}
        candidates = item.get("candidates") or []
        best = candidates[0] if candidates else {}
        best_title = ((best.get("material") or {}).get("title") or (best.get("material") or {}).get("path", ""))
        lines.append(
            f"| {item.get('position', index)} | {_cell(_raw(item))} | {_coverage_text(item)} | "
            f"{_fmt_score(best.get('score', 0))} | {_cell(best_title)} |"
        )

    for index, item in enumerate(hotspots, start=1):
        clue = item.get("clue") or {}
        lines += ["", f"## 二.{item.get('position', index)} 热点：{_raw(item)}", ""]
        lines.extend(f"> {note}" for note in _evidence_notes(item))
        if clue.get("note"):
            lines += [f"> {clue['note']}", ""]

        lines += ["### 为什么能火", ""]
        for why in clue.get("why_it_works") or []:
            lines.append(f"- {why}")

        lines += ["", "### 热点线索（可迁移的爆点要素）", "",
                  "| 类型 | 要素 | 权重 | 置信度 | 依据 |", "| --- | --- | --- | --- | --- |"]
        for element in clue.get("elements") or []:
            lines.append(
                f"| {TYPE_LABELS.get(element.get('type'), element.get('type'))} | {_cell(element.get('value', ''))} | "
                f"{element.get('weight', '')} | {element.get('confidence', '')} | {_cell(element.get('evidence', ''))} |"
            )

        if clue.get("borrow_angles"):
            lines += ["", "### 可蹭角度", ""]
            for angle in clue["borrow_angles"]:
                lines.append(f"- {angle}")

        lines += ["", "### 素材匹配榜", "",
                  "| 排名 | 素材 | 得分 | 命中要素 | 建议用法 |", "| --- | --- | --- | --- | --- |"]
        for candidate in item.get("candidates") or []:
            material = candidate.get("material") or {}
            hit_text = "、".join(
                f"{TYPE_LABELS.get(h.get('element_type'), h.get('element_type'))}:{h.get('clue_value')}"
                for h in candidate.get("hits") or []
            ) or "—"
            lines.append(
                f"| {candidate.get('rank')} | {_cell(material.get('title') or material.get('path', ''))} | "
                f"{_fmt_score(candidate.get('score', 0))} | {_cell(hit_text)} | {_cell(candidate.get('usage', ''))} |"
            )
        if not item.get("candidates"):
            empty = _empty_candidates_note(item)
            lines.append(f"| — | {empty} | — | — | — |")
        for candidate in item.get("candidates") or []:
            lines.append(f"- #{candidate.get('rank')} 匹配理由：{'；'.join(candidate.get('reasons') or [])}")
            lines.extend(f"  - {note}" for note in _evidence_notes(candidate))
            lines.append(f"  - {_preview_note(candidate)}")

        coverage = item.get("coverage") or {}
        lines += ["", f"### 覆盖缺口（{_coverage_text(item)} 的线索有素材）", ""]
        gaps = coverage.get("gaps") or []
        advice = item.get("gap_advice") or []
        if advice:
            # 缺口节点给出的结构化补拍建议优先（S3.1）；缺该键时回退下面的模板句
            for entry in advice:
                label = entry.get("label") or TYPE_LABELS.get(entry.get("type"),
                                                              entry.get("type"))
                lines.append(f"- 缺少 [{label}] {_cell(entry.get('value', ''))} → "
                             f"{_cell(entry.get('advice', ''), 90)}")
        elif (item.get("evidence") or {}).get("gap_advice", {}).get("completeness") == "missing":
            lines.append("- 历史缺口建议缺失，未重新计算")
        elif gaps and not item.get("evidence"):
            for gap in gaps:
                lines.append(f"- 缺少 [{TYPE_LABELS.get(gap.get('type'), gap.get('type'))}] {gap.get('value')} 相关素材 → 建议补拍或去素材库再捞")
        elif _coverage_text(item) == "未知":
            lines.append("- 历史覆盖情况未知")
        elif not gaps:
            lines.append("- 线索要素都有素材覆盖，可以直接剪")

        draft = item.get("draft")
        if draft:
            lines += ["", "### 初步文案", "", draft.get("_markdown") or draft.get("body", "")]
            for warning in draft.get("title_warnings") or []:
                lines.append(f"- ⚠️ {warning}")

    lines += ["", "## 三、本次运行", ""]
    lines += [
        f"- 模型调用：{totals.get('llm_calls', 0)} 次",
        f"- Token：输入 {totals.get('prompt_tokens', 0)} / 输出 {totals.get('completion_tokens', 0)}",
        f"- 预估成本：{totals.get('cost_cny', 0)} 元",
        f"- 总耗时：{totals.get('latency_ms', 0) / 1000:.1f} 秒",
    ]
    vector = meta.get("vector_coverage")
    if vector:
        lines.append(f"- {_vector_coverage_text(vector)}")
    for note in model.get("errors") or []:
        lines.append(f"- ⚠️ {note}")
    lines.extend(f"- {note}" for note in _evidence_notes(model))
    return "\n".join(lines) + "\n"


def render_html(model: dict, base_dir: str = "") -> str:
    meta = model.get("meta") or {}
    cfg = model.get("config") or {}
    totals = model.get("totals") or {}
    hotspots = model.get("hotspots") or []

    cards = []
    for index, item in enumerate(hotspots, start=1):
        clue = item.get("clue") or {}
        candidates = item.get("candidates") or []
        draft = item.get("draft") or {}

        elements = "".join(
            f'<span class="chip chip-{element.get("type", "topic")}">{html.escape(str(TYPE_LABELS.get(element.get("type"), element.get("type"))))}'
            f' · {html.escape(str(element.get("value", "")))}</span>'
            for element in clue.get("elements") or []
        )
        whys = "".join(f"<li>{html.escape(str(w))}</li>" for w in clue.get("why_it_works") or [])
        angles = "".join(f"<li>{html.escape(str(a))}</li>" for a in clue.get("borrow_angles") or [])
        advice = item.get("gap_advice") or []
        if advice:
            gap_html = "".join(
                f'<li>缺少 [{html.escape(str(a.get("label") or a.get("type", "")))}] '
                f'{html.escape(str(a.get("value", "")))} → {html.escape(str(a.get("advice", "")))}</li>'
                for a in advice)
        elif (item.get("evidence") or {}).get("gap_advice", {}).get("completeness") == "missing":
            gap_html = "<li>历史缺口建议缺失，未重新计算</li>"
        elif item.get("evidence"):
            gap_html = ("<li>历史覆盖情况未知</li>" if _coverage_text(item) == "未知"
                        else "<li>本次保存的缺口建议为空</li>")
        else:
            gaps = item.get("coverage", {}).get("gaps") or []
            gap_html = "".join(
                f'<li>[{html.escape(str(TYPE_LABELS.get(g.get("type"), g.get("type"))))}] {html.escape(str(g.get("value", "")))} → 建议补拍</li>'
                for g in gaps
            ) or "<li>线索要素都有素材覆盖</li>"

        rows = []
        for candidate in candidates:
            material = candidate.get("material") or {}
            frame = safe_preview_url(candidate.get("preview_url"))
            current = candidate.get("current_material") or {}
            frame = frame if current.get("preview_available") is True else ""
            preview_note = html.escape(_preview_note(candidate))
            img = (f'<img src="{html.escape(frame)}" alt="历史关键帧" loading="lazy">'
                   if frame else f'<div class="ph">{preview_note}</div>')
            evidence_html = "".join(f'<div class="dim">{html.escape(note)}</div>'
                                    for note in _evidence_notes(candidate))
            reasons = html.escape("；".join(candidate.get("reasons") or []))
            hits = "".join(
                f'<span class="hit">{html.escape(str(TYPE_LABELS.get(h.get("element_type"), h.get("element_type"))))}'
                f' · {html.escape(str(h.get("clue_value", "")))}</span>'
                for h in candidate.get("hits") or []
            ) or '<span class="hit dim">无明确命中</span>'
            rows.append(f"""
            <div class="cand">
              {img}
              <div class="cand-body">
                <div class="cand-head"><b>#{candidate.get('rank')} {html.escape(str(material.get('title') or os.path.basename(str(material.get('path', '')))))}</b>
                  <span class="score">{_fmt_score(candidate.get('score', 0))}</span></div>
                <div class="path">{html.escape(str(material.get('path', '')))}</div>
                <div class="hits">{hits}</div>
                <div class="usage">{html.escape(str(candidate.get('usage', '')))}</div>
                <div class="usage">匹配理由：{reasons}</div>
                {evidence_html}<div class="dim">{preview_note}</div>
              </div>
            </div>""")
        if not rows:
            empty = _empty_candidates_note(item)
            rows.append(f'<p class="dim">{empty}</p>')

        draft_html = ""
        if draft:
            titles = "".join(
                f'<li>{html.escape(str(t.get("text", "")))} <span class="dim">（{html.escape(str(t.get("style", "")))} / {len(str(t.get("text", "")))} 字）</span></li>'
                for t in draft.get("titles") or []
            )
            tags = " ".join(html.escape(str(t)) for t in draft.get("tags") or [])
            shots = "".join(f"<li>{html.escape(str(s))}</li>" for s in draft.get("shot_list") or [])
            notes = "".join(f"<li>{html.escape(str(n))}</li>" for n in draft.get("compliance_notes") or [])
            draft_html = f"""
            <div class="draft">
              <h4>标题备选</h4><ul>{titles}</ul>
              <h4>封面文字</h4><p>{html.escape(str(draft.get('cover_text', '')))}</p>
              <h4>开头 3 秒</h4><p>{html.escape(str(draft.get('first_3s', '')))}</p>
              <h4>正文</h4><p class="body">{html.escape(str(draft.get('body', ''))).replace(chr(10), '<br>')}</p>
              <h4>话题标签</h4><p class="tags">{tags}</p>
              {'<h4>剪辑顺序建议</h4><ol>' + shots + '</ol>' if shots else ''}
              {'<h4>合规提醒</h4><ul>' + notes + '</ul>' if notes else ''}
            </div>"""

        evidence_html = "".join(f'<p class="dim">{html.escape(note)}</p>'
                                for note in _evidence_notes(item))
        cards.append(f"""
        <section class="card">
          <h2><span class="idx">{item.get('position', index)}</span>{html.escape(_raw(item))}</h2>
          {evidence_html}
          <div class="chips">{elements}</div>
          <div class="grid">
            <div><h4>为什么能火</h4><ul>{whys}</ul></div>
            <div><h4>可蹭角度</h4><ul>{angles}</ul></div>
          </div>
          <h4>素材匹配榜</h4>
          {''.join(rows)}
          <h4>覆盖缺口（{_coverage_text(item)} 的线索有素材）</h4>
          <ul>{gap_html}</ul>
          {draft_html}
        </section>""")

    errors = "".join(f"<li>{html.escape(str(e))}</li>" for e in model.get("errors") or [])
    vector = meta.get("vector_coverage")
    vector_html = f" · {html.escape(_vector_coverage_text(vector))}" if vector else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>热点相关性报告 {html.escape(str(meta.get('run_id', '')))}</title>
<style>
:root {{ --ink:#1c1b19; --dim:#7b756c; --line:#e6e0d6; --bg:#f7f4ee; --card:#fffdf9; --accent:#ff2e4d; --accent2:#0f766e; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }}
header {{ padding:28px 32px 12px; }}
header h1 {{ margin:0 0 6px; font-size:24px; }}
.meta {{ color:var(--dim); font-size:13px; }}
main {{ padding:0 32px 64px; max-width:1080px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px 22px; margin:18px 0; }}
.card h2 {{ font-size:20px; margin:0 0 12px; display:flex; align-items:center; gap:10px; }}
.idx {{ display:inline-flex; width:26px; height:26px; border-radius:50%; background:var(--accent); color:#fff; font-size:14px; align-items:center; justify-content:center; }}
h4 {{ margin:16px 0 6px; font-size:14px; color:var(--accent2); }}
.chips {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:6px; }}
.chip {{ background:#f0ece3; border-radius:999px; padding:3px 10px; font-size:12px; }}
.grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
ul,ol {{ margin:6px 0; padding-left:20px; }}
.cand {{ display:flex; gap:14px; padding:12px 0; border-top:1px solid var(--line); }}
.cand img, .ph {{ width:132px; height:88px; object-fit:cover; border-radius:8px; background:#eee; }}
.ph {{ display:flex; align-items:center; justify-content:center; color:var(--dim); font-size:12px; }}
.cand-body {{ flex:1; min-width:0; }}
.cand-head {{ display:flex; justify-content:space-between; gap:10px; align-items:baseline; }}
.score {{ color:var(--accent); font-weight:600; }}
.path {{ color:var(--dim); font-size:12px; word-break:break-all; }}
.hits {{ margin:6px 0; display:flex; flex-wrap:wrap; gap:6px; }}
.hit {{ background:#e8f4f1; color:#0f766e; border-radius:6px; padding:2px 8px; font-size:12px; }}
.hit.dim {{ background:#f1eee8; color:var(--dim); }}
.usage {{ font-size:13px; color:#4a453d; }}
.draft {{ background:#fbf7ef; border:1px dashed var(--line); border-radius:10px; padding:14px 16px; margin-top:14px; }}
.draft .body {{ white-space:normal; }}
.tags {{ color:var(--accent2); }}
.dim {{ color:var(--dim); }}
footer {{ padding:0 32px 40px; color:var(--dim); font-size:13px; }}
@media (max-width:760px) {{ .grid {{ grid-template-columns:1fr; }} .cand {{ flex-direction:column; }} .cand img,.ph {{ width:100%; height:160px; }} main,header,footer {{ padding-left:16px; padding-right:16px; }} }}
</style></head>
<body>
<header>
  <h1>热点相关性报告</h1>
  <div class="meta">
    运行 {html.escape(str(meta.get('run_id', '')))} ·
    {html.escape(str(meta.get('created_at', '')))} ·
    模型 {html.escape(_model_label(cfg))} ·
    素材 {_materials_count(meta)} 条 · 热点 {len(hotspots)} 条
  </div>
</header>
<main>
{''.join(cards) if cards else '<p class="dim">本次没有成功分析的热点。</p>'}
</main>
<footer>
  本次运行：模型调用 {totals.get('llm_calls', 0)} 次 · Token 输入 {totals.get('prompt_tokens', 0)} / 输出 {totals.get('completion_tokens', 0)} ·
  预估成本 {totals.get('cost_cny', 0)} 元 · 总耗时 {totals.get('latency_ms', 0) / 1000:.1f} 秒{vector_html}
  {('<ul>' + errors + '</ul>') if errors else ''}
</footer>
</body></html>
"""


def _cell(text: str, limit: int = 42) -> str:
    text = str(text or "").replace("|", "／").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"
