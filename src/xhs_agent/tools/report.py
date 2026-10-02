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


def _rel(base_dir: str, path: str) -> str:
    if not path:
        return ""
    try:
        rel = os.path.relpath(path, base_dir)
    except ValueError:
        return path
    return rel.replace("\\", "/")


def _fmt_score(score: float) -> str:
    return f"{score * 100:.0f}%"


def _vector_coverage_text(vector: dict) -> str:
    """向量覆盖率文案（Markdown 与 HTML 共用）：`有向量/总数（比例）`，未启用时注明。"""
    total = vector.get("total", 0)
    with_embedding = vector.get("with_embedding", 0)
    note = "" if vector.get("enabled", True) else "（向量召回未启用）"
    return f"向量覆盖率 {with_embedding}/{total}（{_fmt_score(vector.get('ratio', 0))}）{note}"


def render_markdown(model: dict, base_dir: str = "") -> str:
    meta = model.get("meta") or {}
    cfg = model.get("config") or {}
    hotspots = model.get("hotspots") or []
    totals = model.get("totals") or {}

    lines = [
        f"# 热点相关性报告｜{meta.get('run_id', '')}",
        "",
        f"- 生成时间：{meta.get('created_at', '')}",
        f"- 分析模型：{(cfg.get('llm') or {}).get('provider', '')} / {(cfg.get('llm') or {}).get('model', '')}",
        f"- 素材库：`{cfg.get('materials_dir', '')}`（{meta.get('materials_count', 0)} 条）",
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
            f"| {index} | {_cell(clue.get('hotspot_raw', ''))} | {_fmt_score(coverage.get('ratio', 0))} | "
            f"{_fmt_score(best.get('score', 0))} | {_cell(best_title)} |"
        )

    for index, item in enumerate(hotspots, start=1):
        clue = item.get("clue") or {}
        lines += ["", f"## 二.{index} 热点：{clue.get('hotspot_raw', '')}", ""]
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
            lines.append("| — | 没有达到阈值的素材 | — | — | — |")

        coverage = item.get("coverage") or {}
        lines += ["", f"### 覆盖缺口（{_fmt_score(coverage.get('ratio', 0))} 的线索有素材）", ""]
        gaps = coverage.get("gaps") or []
        if gaps:
            for gap in gaps:
                lines.append(f"- 缺少 [{TYPE_LABELS.get(gap.get('type'), gap.get('type'))}] {gap.get('value')} 相关素材 → 建议补拍或去素材库再捞")
        else:
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
    return "\n".join(lines) + "\n"


def render_html(model: dict, base_dir: str = "") -> str:
    meta = model.get("meta") or {}
    cfg = model.get("config") or {}
    totals = model.get("totals") or {}
    hotspots = model.get("hotspots") or []

    cards = []
    for index, item in enumerate(hotspots, start=1):
        clue = item.get("clue") or {}
        coverage = item.get("coverage") or {}
        candidates = item.get("candidates") or []
        draft = item.get("draft") or {}

        elements = "".join(
            f'<span class="chip chip-{element.get("type", "topic")}">{html.escape(str(TYPE_LABELS.get(element.get("type"), element.get("type"))))}'
            f' · {html.escape(str(element.get("value", "")))}</span>'
            for element in clue.get("elements") or []
        )
        whys = "".join(f"<li>{html.escape(str(w))}</li>" for w in clue.get("why_it_works") or [])
        angles = "".join(f"<li>{html.escape(str(a))}</li>" for a in clue.get("borrow_angles") or [])
        gaps = item.get("coverage", {}).get("gaps") or []
        gap_html = "".join(
            f'<li>[{html.escape(str(TYPE_LABELS.get(g.get("type"), g.get("type"))))}] {html.escape(str(g.get("value", "")))} → 建议补拍</li>'
            for g in gaps
        ) or "<li>线索要素都有素材覆盖</li>"

        rows = []
        for candidate in candidates:
            material = candidate.get("material") or {}
            frame = (material.get("keyframes") or [""])[0]
            img = f'<img src="{html.escape(_rel(base_dir, frame))}" alt="">' if frame else '<div class="ph">无预览</div>'
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
              </div>
            </div>""")
        if not rows:
            rows.append('<p class="dim">没有达到阈值的素材，建议放宽阈值或补充素材。</p>')

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

        cards.append(f"""
        <section class="card">
          <h2><span class="idx">{index}</span>{html.escape(str(clue.get('hotspot_raw', '')))}</h2>
          <div class="chips">{elements}</div>
          <div class="grid">
            <div><h4>为什么能火</h4><ul>{whys}</ul></div>
            <div><h4>可蹭角度</h4><ul>{angles}</ul></div>
          </div>
          <h4>素材匹配榜</h4>
          {''.join(rows)}
          <h4>覆盖缺口（{_fmt_score(coverage.get('ratio', 0))} 的线索有素材）</h4>
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
    模型 {html.escape(str((cfg.get('llm') or {}).get('provider', '')))}/{html.escape(str((cfg.get('llm') or {}).get('model', '')))} ·
    素材 {meta.get('materials_count', 0)} 条 · 热点 {len(hotspots)} 条
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
