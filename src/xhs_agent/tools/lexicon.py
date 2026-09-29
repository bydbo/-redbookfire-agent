"""爆点要素词典。

作用有两个：
1. 离线/降级模式下，没有大模型也能把热点和素材拆成带类型的要素；
2. 在线模式下，给素材打标签兜底（大模型只负责它更擅长的那部分）。
"""

from __future__ import annotations

from ..schemas import DEFAULT_TYPE_WEIGHTS, Element
from ..util import normalize_text, truncate

RAW_LEXICON = {
    "topic": [
        ["羽毛球", ["羽毛球", "球拍", "挥拍", "杀球", "双打", "扣杀", "球场挥汗"]],
        ["篮球", ["篮球", "三分球", "扣篮", "野球场"]],
        ["跑步", ["跑步", "晨跑", "夜跑", "马拉松", "配速"]],
        ["健身", ["健身", "撸铁", "增肌", "举铁", "深蹲", "弹力带"]],
        ["游泳", ["游泳", "泳池", "自由泳"]],
        ["露营", ["露营", "天幕", "帐篷", "野餐"]],
        ["徒步登山", ["徒步", "爬山", "登山", "山顶"]],
        ["骑行", ["骑行", "单车", "公路车"]],
        ["滑雪", ["滑雪", "雪场", "单板"]],
        ["瑜伽普拉提", ["瑜伽", "普拉提", "体态"]],
        ["穿搭", ["穿搭", "ootd", "显瘦", "显高", "通勤穿搭"]],
        ["护肤", ["护肤", "水乳", "精华", "敏感肌", "痘痘", "防晒"]],
        ["美妆", ["美妆", "化妆", "口红", "眼妆", "伪素颜"]],
        ["发型", ["发型", "剪发", "烫发", "高颅顶"]],
        ["美食探店", ["美食", "探店", "苍蝇馆子", "餐厅"]],
        ["家常菜", ["家常菜", "菜谱", "做饭", "下厨", "一锅出"]],
        ["烘焙", ["烘焙", "蛋糕", "面包", "甜品"]],
        ["减脂餐", ["减脂餐", "低卡", "控糖", "轻食", "健康餐"]],
        ["咖啡", ["咖啡", "拿铁", "手冲", "咖啡店"]],
        ["旅行", ["旅行", "旅游", "出片", "旅游攻略", "citywalk"]],
        ["猫咪", ["猫咪", "撸猫", "猫奴", "橘猫", "猫"]],
        ["狗狗", ["狗狗", "柯基", "柴犬", "遛狗"]],
        ["职场", ["职场", "上班", "打工", "工位", "加班", "同事", "面试", "跳槽", "简历"]],
        ["考研考公", ["考研", "考公", "上岸", "备考", "自习"]],
        ["学生生活", ["学生", "大学生", "宿舍", "军训", "期末"]],
        ["母婴育儿", ["宝宝", "育儿", "奶粉", "辅食", "带娃"]],
        ["家居收纳", ["收纳", "出租屋", "改造", "家装", "置物"]],
        ["数码", ["数码", "手机", "相机", "耳机", "键盘"]],
        ["摄影", ["摄影", "拍照", "构图", "手机摄影"]],
        ["手作", ["手工", "手作", "diy"]],
        ["游戏", ["游戏", "手游", "上分", "电竞"]],
        ["减肥", ["减肥", "瘦身", "掉秤", "暴瘦"]],
        ["情绪疗愈", ["疗愈", "内耗", "放松", "spa"]],
        ["副业赚钱", ["副业", "搞钱", "赚钱"]],
    ],
    "ip": [
        ["明星艺人", ["明星", "艺人", "爱豆", "顶流", "偶像", "歌手", "演员", "idol", "代言"]],
        ["网红达人", ["网红", "博主", "达人", "大v", "up主", "主播"]],
        ["企业家", ["企业家", "老板", "ceo", "创始人", "总裁"]],
        ["运动员", ["运动员", "冠军", "国家队", "选手"]],
        ["素人", ["素人", "普通人", "路人"]],
        ["专业人士", ["医生", "律师", "老师", "营养师", "教练", "专家"]],
        ["萌宠IP", ["网红猫", "萌宠"]],
    ],
    "scene": [
        ["球场", ["球场", "体育馆", "球馆", "场地"]],
        ["健身房", ["健身房", "器械区", "瑜伽房"]],
        ["户外山野", ["户外", "山里", "野外", "山顶", "海边", "公园", "草地"]],
        ["居家", ["家里", "居家", "卧室", "客厅", "厨房", "阳台"]],
        ["办公室通勤", ["办公室", "工位", "会议室", "通勤", "地铁", "公交"]],
        ["车内", ["车里", "车内", "副驾"]],
        ["门店", ["店内", "店里", "门店", "柜台"]],
        ["学校", ["学校", "教室", "图书馆"]],
        ["旅途中", ["机场", "高铁", "酒店", "民宿"]],
    ],
    "emotion": [
        ["反差", ["反差", "没想到", "居然", "竟然", "反差感"]],
        ["治愈", ["治愈", "温暖", "舒服", "惬意"]],
        ["解压", ["解压", "好爽", "爽感", "松弛"]],
        ["共鸣", ["共鸣", "扎心", "真实", "破防", "懂得都懂"]],
        ["热血", ["热血", "燃", "坚持", "不服输"]],
        ["好笑", ["好笑", "笑死", "搞笑", "离谱"]],
        ["羡慕", ["羡慕", "慕了", "向往"]],
        ["焦虑", ["焦虑", "内耗", "emo", "压力"]],
    ],
    "visual": [
        ["慢动作", ["慢动作", "慢镜", "升格"]],
        ["特写", ["特写", "细节", "微距"]],
        ["对比", ["对比", "前后对比", "分屏"]],
        ["第一视角", ["第一视角", "pov", "主观视角"]],
        ["氛围空镜", ["空镜", "画面感", "氛围感"]],
        ["快剪卡点", ["快剪", "卡点"]],
    ],
    "sound": [
        ["BGM", ["bgm", "配乐", "音乐", "神曲"]],
        ["原声", ["原声", "现场音", "环境音"]],
        ["口播", ["口播", "讲解", "旁白"]],
    ],
    "conflict": [
        ["身份反差", ["身份反差", "外表vs实际", "别看"]],
        ["预期违背", ["以为", "结果", "翻车"]],
        ["低门槛", ["零基础", "不用基础", "小白也能"]],
    ],
    "format": [
        ["同款", ["同款", "get同款", "复刻"]],
        ["跟练", ["跟练", "打卡", "一起练"]],
        ["教程", ["教程", "步骤", "保姆级", "从零"]],
        ["测评", ["测评", "实测", "红黑榜"]],
        ["挑战", ["挑战", "七天", "30天"]],
        ["清单合集", ["合集", "清单", "盘点"]],
    ],
    "audience": [
        ["打工人", ["打工人", "上班族", "社畜"]],
        ["学生党", ["学生党", "大学生", "考研党"]],
        ["宝妈", ["宝妈", "妈妈", "带娃"]],
        ["新手小白", ["新手", "小白", "零基础"]],
        ["精致女孩", ["姐妹", "女生", "女孩子"]],
        ["男生", ["男生", "兄弟", "哥们"]],
    ],
}

# 预处理：把同义词统一归一化，方便直接做子串匹配
LEXICON: dict = {}
_SURFACE_INDEX: list = []
for _type, _entries in RAW_LEXICON.items():
    LEXICON[_type] = []
    for _value, _surfaces in _entries:
        norm_surfaces = {normalize_text(s) for s in _surfaces} | {normalize_text(_value)}
        LEXICON[_type].append({"value": _value, "surfaces": sorted(norm_surfaces, key=len, reverse=True)})
        for _s in norm_surfaces:
            if _s:
                _SURFACE_INDEX.append((_s, _type, _value))
_SURFACE_INDEX.sort(key=lambda item: len(item[0]), reverse=True)


def values_of(element_type: str) -> list:
    return [entry["value"] for entry in LEXICON.get(element_type, [])]


def all_values() -> list:
    out = []
    for entries in LEXICON.values():
        out.extend(entry["value"] for entry in entries)
    return out


def detect(text: str) -> list:
    """返回文本中命中的 (type, value, surface) 列表。"""
    norm = normalize_text(text)
    if not norm:
        return []
    hits = []
    seen = set()
    for surface, element_type, value in _SURFACE_INDEX:
        if len(surface) < 2:
            continue
        if surface in norm and (element_type, value) not in seen:
            seen.add((element_type, value))
            hits.append((element_type, value, surface))
    return hits


def elements_from_text(text: str, max_per_type: int = 3, generic_fallback: bool = True) -> list:
    """把一段文本拆成带类型的爆点要素。"""
    hits = detect(text)
    grouped: dict = {}
    for element_type, value, surface in hits:
        bucket = grouped.setdefault(element_type, [])
        if value not in [item[0] for item in bucket]:
            bucket.append((value, surface))

    elements = []
    for element_type, bucket in grouped.items():
        base_weight = DEFAULT_TYPE_WEIGHTS.get(element_type, 0.5)
        for index, (value, surface) in enumerate(bucket[:max_per_type]):
            elements.append(Element(
                type=element_type,
                value=value,
                weight=max(0.25, base_weight - 0.1 * index),
                confidence=0.75 if index == 0 else 0.6,
                evidence=surface,
            ))

    if not elements and generic_fallback:
        head = normalize_text(_first_chunk(text))[:12]
        if head:
            elements.append(Element(type="topic", value=head, weight=0.55, confidence=0.4, evidence="原文片段"))
    return elements


def elements_from_tags(tags: list, description: str = "", title: str = "") -> list:
    """素材标签 -> 要素。词典命中的标签置信度更高，未命中的按主题处理。"""
    elements = []
    seen = set()

    for tag in tags:
        tag = str(tag).strip()
        if not tag:
            continue
        hits = detect(tag)
        for element_type, value, _surface in hits:
            key = (element_type, value)
            if key not in seen:
                seen.add(key)
                elements.append(Element(type=element_type, value=value, weight=DEFAULT_TYPE_WEIGHTS.get(element_type, 0.5),
                                        confidence=0.85, evidence=f"标签：{tag}"))

    for text, confidence, source in ((title, 0.6, "标题"), (description, 0.65, "描述")):
        for element_type, value, surface in detect(text):
            key = (element_type, value)
            if key not in seen:
                seen.add(key)
                elements.append(Element(type=element_type, value=value,
                                        weight=DEFAULT_TYPE_WEIGHTS.get(element_type, 0.5),
                                        confidence=confidence, evidence=f"{source}：{surface}"))

    # 未被词典识别的自定义标签，按主题要素保留，避免「用户自己写的标签」被丢掉
    for tag in tags:
        tag = str(tag).strip()
        if len(tag) < 2 or len(tag) > 12 or detect(tag):
            continue
        if any(normalize_text(tag) == normalize_text(e.value) for e in elements):
            continue
        elements.append(Element(type="topic", value=tag, weight=0.55, confidence=0.5, evidence="自定义标签"))
    return elements[:16]


def keywords_from_text(text: str, limit: int = 12) -> list:
    """从文本里抽出可用来做关键词命中的词，词典命中优先。"""
    keywords = [value for _type, value, _surface in detect(text)]
    norm = normalize_text(text)
    for chunk in _chunks(text):
        chunk_norm = normalize_text(chunk)
        if 2 <= len(chunk_norm) <= 12 and chunk_norm not in [normalize_text(k) for k in keywords]:
            keywords.append(chunk.strip())
    out, seen = [], set()
    for key in keywords:
        norm_key = normalize_text(key)
        if norm_key and norm_key not in seen and len(norm_key) >= 2:
            seen.add(norm_key)
            out.append(key.strip())
    return out[:limit]


def _chunks(text: str) -> list:
    raw = str(text or "")
    for sep in ["，", ",", "。", "；", ";", "、", "：", ":", "！", "!", "？", "?", "|", "\n", " ", "\u3000"]:
        raw = raw.replace(sep, "\x00")
    return [part for part in raw.split("\x00") if part.strip()]


def _first_chunk(text: str) -> str:
    chunks = _chunks(text)
    return truncate(chunks[0], 20, suffix="") if chunks else str(text or "")[:20]