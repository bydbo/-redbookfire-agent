"""离线规则引擎。

没有 API Key 时接管所有 Agent 的判断，保证「链路可跑、产物可看」。
它产出的结论会明确标注为规则推断，不会被误当成大模型分析结果。
"""

from __future__ import annotations

from . import lexicon

OFFLINE_NOTE = "离线规则引擎生成（未调用大模型），仅用于跑通链路与自测"


def _title(text: str, limit: int = 20) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit]


def _by_type(elements: list, element_type: str) -> list:
    return [e for e in elements if e.type == element_type]


def _first(elements: list, element_type: str, default: str = "") -> str:
    bucket = _by_type(elements, element_type)
    return bucket[0].value if bucket else default


def hotspot_clue(hotspot_raw: str) -> dict:
    """把热点拆成线索（规则版）。"""
    elements = lexicon.elements_from_text(hotspot_raw)
    if not elements:
        elements = lexicon.elements_from_text(hotspot_raw, generic_fallback=True)

    topic = _first(elements, "topic", "这个主题")
    scene = _first(elements, "scene", "手边的场景")
    ip = _first(elements, "ip", "")
    emotion = _first(elements, "emotion", "真实")
    fmt = _first(elements, "format", "")
    audience = _first(elements, "audience", "想试又怕门槛高的新手")

    mechanisms = []
    conflict = _by_type(elements, "conflict")
    if conflict or emotion in {"反差", "好笑"}:
        mechanisms.append({"name": "反差感", "explain": "画面或结论先违反预期，观众会停下来确认自己有没有看错"})
    if ip:
        mechanisms.append({"name": "熟人IP注意力捷径", "explain": f"{ip}自带认知度，观众不用被科普就直接进入内容"})
    if fmt in {"跟练", "教程", "低门槛"} or any(e.type == "conflict" and e.value == "低门槛" for e in elements):
        mechanisms.append({"name": "参与门槛低", "explain": "看起来普通人也能做，观众容易产生「我也可以」的代入"})
    if emotion in {"解压", "治愈", "羡慕"}:
        mechanisms.append({"name": "情绪供给", "explain": f"{emotion}是直接的刷屏动机，画面本身就是情绪出口"})
    if not mechanisms:
        mechanisms.append({"name": "信息密度高", "explain": "前三秒给出明确画面或结论，减少划走概率"})

    why = [f"{m['name']}：{m['explain']}" for m in mechanisms][:3]
    why.append("（" + OFFLINE_NOTE + "）")

    keyword_pool = [topic, scene, emotion, fmt, audience] + [e.value for e in elements]
    keywords = []
    for item in keyword_pool:
        if item and item not in keywords:
            keywords.append(item)
    keywords.extend(lexicon.keywords_from_text(hotspot_raw, limit=8))

    angles = [f"用自己的{scene}画面复刻「{topic}」，把明星/热点里的动作或流程换成你的日常版本"]
    if ip:
        angles.append(f"以「{audience}视角体验{topic}」切入，借{ip}的注意力但不直接使用其肖像")
    angles.append(f"做一个「我以为是{emotion}，结果…」的反差开头，把{topic}过程真实拍下来")
    angles.append(f"把{topic}拆成 3 步{topic}入门清单，做成可收藏的干货")

    risks = ["蹭热点请避免使用他人姓名、肖像和未授权素材，用「同款」「原型」等表述替代"]
    if ip:
        risks.append(f"涉及{ip}时不要暗示商业合作或代言关系")

    return {
        "hotspot_raw": hotspot_raw.strip(),
        "why_it_works": why,
        "mechanisms": mechanisms,
        "elements": [e.to_dict() for e in elements],
        "match_keywords": keywords[:14],
        "audience": {
            "core": audience,
            "pain_points": [f"想蹭{topic}但不确定自己素材能不能用", "做了没有起色，不知道差在哪一步"],
        },
        "borrow_angles": angles[:4],
        "risk_notes": risks,
        "note": OFFLINE_NOTE,
    }


def copy_draft(clue: dict, material: dict, style: str = "") -> dict:
    """规则版文案初稿：结构完整、可直接改，不用等大模型。"""
    elements = [e for e in (clue.get("elements") or []) if isinstance(e, dict)]
    topic = _first_raw(elements, "topic", "这个话题")
    scene = _first_raw(elements, "scene", "现场")
    emotion = _first_raw(elements, "emotion", "意外")
    audience = _first_raw(elements, "audience", "新手")
    hotspot = str(clue.get("hotspot_raw") or "最近的热点").strip()
    hotspot_short = hotspot if len(hotspot) <= 18 else hotspot[:18]
    style = (style or "真诚分享").strip()

    material_title = str(material.get("title") or material.get("description") or "").strip()
    material_hint = f"（我手里这条素材是：{material_title}）" if material_title else ""

    titles = [
        {"text": _title(f"{topic}新手也能上手的版本"), "style": "干货"},
        {"text": _title(f"我复刻了{topic}，结果{emotion}"), "style": "反差"},
        {"text": _title(f"{audience}别硬冲{topic}"), "style": "避坑"},
    ]

    body = (
        f"最近{hotspot_short}刷屏，我一开始也以为只是热闹，自己上手试了几天才发现：{topic}真正上头的点不是结果，是过程里那种「我居然真的做到了」的感觉。\n\n"
        f"这次我没买什么装备，就在{scene}完成的{material_hint}。整体感受有三点：\n"
        f"1. 门槛比想象中低，第一次做得很粗糙也不影响出片；\n"
        f"2. 关键是{emotion}那一下要拍清楚，观众要看到变化；\n"
        f"3. 收尾别急着讲道理，留一个问题让对方来评论。\n\n"
        f"如果你也是{audience}，建议先照着做一次最简版，别一上来就追求完美。\n\n"
        f"你会想先试哪一步？评论区告诉我，下次我把完整流程拍出来。\n\n"
        f"（文案风格：{style}；{OFFLINE_NOTE}）"
    )

    tags = []
    for item in [topic, scene, emotion] + [str(k) for k in (clue.get("match_keywords") or [])]:
        tag = f"#{item}"
        if item and tag not in tags:
            tags.append(tag)
    tags = tags[:10]

    return {
        "material_id": material.get("id", ""),
        "hotspot_key": clue.get("hotspot_key", ""),
        "titles": titles,
        "body": body,
        "tags": tags,
        "cover_text": _title(f"{topic} 我试了"),
        "first_3s": f"开场直接给{emotion}的画面，第一句话：我以为{topic}只有{audience}才玩得动。",
        "shot_list": [
            f"0-3 秒：{scene}里最有冲击力的一帧，先给结果或反差",
            f"3-10 秒：放准备工作，交代「{audience}也能上手」",
            "10-25 秒：主过程，保留真实失误和反应",
            f"25-35 秒：{emotion}的收尾镜头 + 一句提问引导评论",
        ],
        "compliance_notes": list(clue.get("risk_notes") or []) + [
            "人名、肖像、商标素材需自行确认授权；本工具只做相关性建议",
        ],
        "note": OFFLINE_NOTE,
    }


def _first_raw(elements: list, element_type: str, default: str) -> str:
    for item in elements:
        if str(item.get("type")) == element_type and item.get("value"):
            return str(item["value"])
    return default


def material_explain(clue: dict, material: dict, hits: list) -> dict:
    """规则版匹配解释：说清「为什么这条素材相关」。"""
    reasons = []
    for hit in hits[:3]:
        reasons.append(f"命中{_label(hit.get('element_type'))}：{hit.get('clue_value')} ↔ 素材的「{hit.get('hit_value')}」")
    if not reasons:
        reasons.append("没有明显命中，只是兜底候选")

    topic = "这个热点"
    for item in (clue.get("elements") or []):
        if isinstance(item, dict) and item.get("type") == "topic":
            topic = str(item.get("value"))
            break
    usage = f"可用作{topic}的实拍素材，建议放在开头 3 秒或作为过程画面"
    if material.get("duration_s"):
        usage += f"（时长 {material.get('duration_s')} 秒）"
    return {"reasons": reasons, "usage": usage}


def _label(element_type) -> str:
    from ..schemas import TYPE_LABELS

    return TYPE_LABELS.get(str(element_type), str(element_type))