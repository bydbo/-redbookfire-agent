"""对话可调用的工具（S7.3 起；S7.7 加只读三件，S7.7b 加拆解预览，S7.8 加改写；PR2 加网页两件）。

用途：把「对话层能做的事」写成工具——跑完整分析（第一批）、解析图片 / 查素材库 / 读历史运行
      （S7.7 只读三件）、预览爆点要素与缺口（S7.7b）、改写文案（S7.8）、搜热点 / 读网页
      （PR2 的 `search_hotspots` / `fetch_url`，默认不下发）。
输入：`ToolContext`（配置 / 会话 / 投递器 / 文本模型 caller / 可注入的连接池与 sleep）+ 工具参数 dict。
输出：`ToolOutcome`（status / payload / steps / error / prompt_versions / 用量）——payload 既喂回
      给模型，也落进 `chat_messages.tool_calls`。

口径：

- **工具定义都在本文件，执行分派（`HANDLERS`）也在本文件**（S7.7a）：给模型的 function
  schema 与 name → handler 映射都集中在这里——`TOOLS` 是默认清单，网页两件的 schema 是
  下面的 `TOOL_SCHEMA_*`。`workflows/chat.py` 里不出现任何具体工具名——「新增一个工具只改
  `chat_tools.py` 一处」。prompt 契约管的是 `prompts/` 下的 prompt 正文；工具的
  name/description/参数属于 API 协议，随代码走，不抄一份进 prompt；
- **清单按开关下发**（PR2）：`TOOLS` 是**默认清单**——`[web].enabled=false` 时 `tools_for(cfg)`
  返回的清单与它**逐项相等**，网页能力开启才追加；因此网页两件的 schema 放在 `TOOLS` 外面。
  `tool_names(cfg)` 跟着同一份清单走（图按**本轮完整清单**划分已知 / 未知工具），
  `effective_max_rounds(cfg)` 在网页能力开启时把工具轮次提到**至少** `WEB_MAX_TOOL_ROUNDS`
  （取 `max`：用户自己设得更大就按用户的）——「搜完再读、读完接着读」不是 3 轮能完成的；
- **只读的有哪些**：`parse_image_clue`（图片只在内存里变成模型输入，不落盘不入库）、
  `search_materials`（只检索）、`read_run`（只读、不花钱）、`preview_element_coverage`
  （不调 embedding、不写稿、不落 runs）、`search_hotspots`（只发一次搜索请求，不花钱、
  不写库）、`fetch_url`（只读一个公网页，受 SSRF 校验与 robots 约束，不落盘）。
  有副作用的只有 `run_hotspot_analysis`（投 job）与 `revise_draft`（花一次模型钱，
  但**不写库**——run 的初稿快照不动）；
- **一轮内不重复付费**：`ToolContext.cache` 按「热点原文」缓存拆解结果、按「附件路径」缓存
  图片解析结果，所以「先预览再检索」「先解析图片再跑分析」都只付一次模型钱；
- **`run_id` 可省**（`read_run` / `revise_draft`）：不传就落到本会话**最近一次**运行产出的
  那个（`services/chat.py::latest_run_id`）——装配给模型的历史只有消息正文，工具返回值里的 id
  它看不到，用户说「上次那批」「刚才那版」时只能由服务端兜底（S7.9 冒烟实测到这条）；
- **跨轮图片回落**（S7.10 / S7.16 按组）：这一轮没有附件时，图片类工具用
  `ToolContext.recent_attachments`（本会话**最近一组**图，`services/chat.py::latest_attachments`）
  ——用户说「上面那几张图」才用得上。`run_hotspot_analysis` **只在热点文本为空时**才用它，
  避免拿旧图线索去跑一条新热点；
- **一组图一次解析**（S7.16）：1 张走单图 prompt、多张走合并 prompt，都只调一次多模态接口；
  同一组在本轮内只解析一次（缓存键是整组路径），成本只记那一次；
- 有图片附件时先走 S6.6 的 `parse_image_clue`（多模态），把解析出的线索作为**预置线索**
  随 `submit_analysis(clues=...)` 提交——拆解节点短路，不重复付费（S6.7）；
- 分析在 Celery worker 跑，本工具只等结果：轮询 `runs`（ADR 0011：库是权威源），
  上限 `[queue].task_time_limit_s`；同一 session 里轮询前必须 `expire_all()`，否则读到旧对象；
- **成本与用量记账**：文本模型调用的成本/用量取 `StructuredCaller.records` 的增量，写进
  `payload["cost_cny"]` 与 `payload["prompt_tokens"] / ["completion_tokens"]`，同时落到
  `ToolOutcome` 的用量字段（由对话图累加进这一轮）；**S7.10 起多模态图片解析也计入**
  （S6.6 的「图片解析不计成本」临时口径作废，单价在 `[vision]`）；
  分析工具的用量取那次 run 的 `totals`；`read_run` 不花钱；
- 参数不合法 / 上游失败一律返回 `status=failed` + 一句能直接回给用户的原因，不抛异常
  （抛出去会触发节点重试，可能重复付费）。网页两件同口径：搜索实例不通、页面抓不到、
  robots 拒绝、本轮调用次数用完，都只是 `failed` + 中文原因，既不抛也不静默降级。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents.clue import TASK_ID as CLUE_TASK
from ..agents.clue import extract_clue
from ..agents.revise import TASK_ID as REVISE_TASK
from ..agents.revise import ReviseOutcome
from ..agents.revise import revise_draft as revise_agent
from ..config import AppConfig
from ..core import context_rules
from ..core import progress as progress_tool
from ..core.errors import ApiError, BadRequestError, ConflictError, public_error_message
from ..core.observations import record as record_observation
from ..core.tracing import LEVEL_ERROR, get_tracer
from ..db.models import ChatArtifactVersion, ChatMessage
from ..schemas import TYPE_LABELS, Element, HotspotClue, SchemaError
from ..services import artifact_versions, context_state, image_cache
from ..services.chat import latest_run_id, resolve_attachment
from ..services.image_clue import (
    ImageClueParse,
    parse_image_clue,
    parse_image_clue_multi,
)
from ..services.retrieval import literal_only_candidates, retrieve_candidates
from ..services.run_snapshots import (
    ORIGINS,
    digest,
    evidence_meta,
    text_evidence_missing,
)
from ..services.runs import load_job, load_run, submit_analysis
from ..tools.embedding import EmbeddingError
from ..tools.gaps import gap_advice
from ..tools.llm import LLMError, StructuredCaller
from ..tools.vision import sniff_image_mime
from ..tools.webreader import WebFetchError, fetch_readable
from ..tools.websearch import WebSearchError, search_web
from .material_identity import canonical_id, current_material


async def _canonical_base_material(session: Any, base_draft: dict[str, Any]) -> dict[str, Any]:
    """基础稿持有 E6 旧 UUID 时，经已验证别名映射到规范 ID；不改写版本正文。"""
    if session is None:
        return base_draft
    try:
        old_id = uuid.UUID(str(base_draft.get("material_id") or ""))
    except (TypeError, ValueError):
        return base_draft
    resolved = await canonical_id(session, old_id)
    return {**base_draft, "material_id": str(resolved),
            "_original_material_id": str(base_draft.get("_original_material_id") or old_id)}



def _historical_evidence(
    *, item: dict[str, Any], base_draft: dict[str, Any], run_id: str,
    position: int, base_version: Any = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """为改稿定位唯一历史证据：基础版本自身 > 本运行冻结快照；缺失则明确拒绝。

    返回 `(evidence, missing_fields)`；不读取当前素材、不猜第一候选或最新运行。
    """
    material_id = str(base_draft.get("material_id") or "")
    candidates = list(item.get("candidates") or [])
    matches = [row for row in candidates if str(row.get("material_id") or "") == material_id]
    # A version's evidence describes the input used to create that version. Validate it,
    # then bind this request to the selected version's own content rather than its parent draft.
    clue: Any
    stored = dict(getattr(base_version, "evidence_snapshot", None) or {})
    if stored:
        stored_missing = _evidence_missing(stored, run_id=run_id, position=position)
        if stored_missing:
            return None, stored_missing
        canonical = str(stored.get("canonical_material_id") or material_id)
        if canonical != material_id:
            return None, ["material_identity"]
        clue = dict(stored["clue"])
        material = dict(stored["material"])
        sources = dict(stored["sources"])
    else:
        clue = item.get("clue")
        material = dict(matches[0].get("material") or {}) if len(matches) == 1 else {}
        sources = {
            "clue": (item.get("evidence") or {}).get("clue") or {},
            "material": (matches[0].get("evidence") or {}).get("candidate") or {}
            if len(matches) == 1 else {},
        }
    missing = text_evidence_missing(clue, material, base_draft)
    original_draft_id = str(base_draft.get("_original_material_id") or material_id)
    if not stored and original_draft_id != str(material.get("original_material_id") or material.get("id") or ""):
        missing.append("draft_material_provenance")
    for key, value in (("clue", clue), ("material", material)):
        if not _verified_group(sources.get(key), value):
            missing.append(f"sources.{key}")
    if not stored and len(matches) != 1:
        missing.append("material_identity")
    if missing:
        return None, missing
    origins = [str(sources[key]["origin"]) for key in ("clue", "material")]
    origin = "runtime" if all(value == "runtime" for value in origins) else "legacy_run_record"
    current = dict(matches[0].get("current_material") or {}) if len(matches) == 1 else {}
    payload = {
        "schema_version": 1,
        "origin": origin,
        "captured_at": evidence_meta(
            origin=origin, source_ref=f"run:{run_id}/position:{position}",
            completeness="complete", payload={})["captured_at"],
        "source_ref": f"run:{run_id}/position:{position}",
        "digest": digest({"clue": clue, "material": material, "base_draft": base_draft}),
        "completeness": "complete",
        "missing_fields": [],
        "clue": clue,
        "material": material,
        "base_draft": base_draft,
        "canonical_material_id": material_id,
        "draft_original_material_id": original_draft_id,
        "sources": sources,
        "current_material": current,
    }
    return payload, []


def _verified_group(meta: Any, payload: Any) -> bool:
    return (isinstance(meta, dict) and meta.get("schema_version") == 1
            and meta.get("origin") in ORIGINS and meta.get("origin") != "missing"
            and meta.get("completeness") == "complete" and not meta.get("missing_fields")
            and bool(meta.get("source_ref")) and bool(meta.get("captured_at"))
            and meta.get("digest") == digest(payload))


def _evidence_missing(stored: dict[str, Any], *, run_id: str, position: int) -> list[str]:
    missing = text_evidence_missing(stored.get("clue"), stored.get("material"),
                                     stored.get("base_draft"))
    payload = {key: stored.get(key) for key in ("clue", "material", "base_draft")}
    if (stored.get("schema_version") != 1 or stored.get("completeness") != "complete"
            or stored.get("missing_fields") or stored.get("digest") != digest(payload)
            or stored.get("origin") not in ORIGINS or stored.get("origin") == "missing"
            or not stored.get("captured_at")
            or stored.get("source_ref") != f"run:{run_id}/position:{position}"):
        missing.append("evidence_snapshot")
    sources = stored.get("sources") or {}
    for key in ("clue", "material"):
        if not _verified_group(sources.get(key), stored.get(key)):
            missing.append(f"sources.{key}")
    return missing


async def _bound_historical_evidence(
    session: Any, *, item: dict[str, Any], base_draft: dict[str, Any], run_id: str,
    position: int, base_version: Any = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """验证原证据 UUID 与规范历史 UUID 等价；动态状态只从当前读取，不信旧版本缓存。"""
    evidence, missing = _historical_evidence(
        item=item, base_draft=base_draft, run_id=run_id,
        position=position, base_version=base_version)
    if evidence is None:
        return None, missing
    canonical = uuid.UUID(str(base_draft["material_id"]))
    original = uuid.UUID(str(evidence["material"]["id"]))
    if session is None:
        if original != canonical:
            return None, ["material_identity"]
    else:
        if await canonical_id(session, original) != canonical:
            return None, ["material_identity"]
        current = await current_material(session, canonical)
        evidence["current_material"] = {"exists": current is not None}
    return evidence, []



TOOL_RUN_ANALYSIS = "run_hotspot_analysis"
TOOL_PARSE_IMAGE = "parse_image_clue"
TOOL_SEARCH_MATERIALS = "search_materials"
TOOL_READ_RUN = "read_run"
TOOL_PREVIEW_COVERAGE = "preview_element_coverage"
TOOL_REVISE_DRAFT = "revise_draft"
# 网页两件（PR2）：schema 在 `TOOLS` 之外，由 `tools_for` 按 `[web]` 开关追加
TOOL_SEARCH_HOTSPOTS = "search_hotspots"
TOOL_FETCH_URL = "fetch_url"

logger = logging.getLogger("xhs_agent.chat_tools")

# 运行终态：出现在这里就是"这一批跑完了"（与 `tasks/analysis.py` 同一口径）
RUN_TERMINAL = ("succeeded", "failed")

# 热点原文上限（与 `POST /api/analyze` 的 AnalyzeRequest 同口径）
MAX_HOTSPOT_CHARS = 500
# 修改要求上限（与热点原文同口径；模型看上下文有限，太长反而抓不到重点）
MAX_REQUEST_CHARS = 500
MAX_TOPK = 20
# 回给模型的候选条数上限：再多也是噪声，且会挤占对话上下文
CANDIDATE_PREVIEW_LIMIT = 8
TAG_PREVIEW_LIMIT = 6
HIT_PREVIEW_LIMIT = 3
# 网页能力开启后一轮最多几次工具调用：搜 → 读 → 接着读，`[chat]` 的默认 3 轮不够
WEB_MAX_TOOL_ROUNDS = 8
# 搜索方向的一句话上限、一次搜索的默认 / 最大条数（与 `TOOL_SCHEMA_SEARCH_HOTSPOTS` 同口径）
MAX_QUERY_CHARS = 80
DEFAULT_SEARCH_COUNT = 5
MAX_SEARCH_COUNT = 10
# 本轮网页工具配额用完时的统一文案：直接回给用户也说得通，模型看了会收手
TURN_LIMIT_MESSAGE = "本轮网页工具已用完，请用现有结果作答"
# 只放行 http(s)：file:// / javascript: 之类在进适配器之前就该拒（与两个适配器同口径）
WEB_URL_SCHEMES = ("http", "https")

# 给模型的**默认**工具清单（OpenAI 兼容的 function schema）。默认清单的唯一来源，别在别处再
# 抄一份；网页两件默认不下发，schema 在下面 `TOOL_SCHEMA_*`，由 `tools_for` 按开关追加。
# description 里写"什么时候用它"——prompt 的 03 Constraints 只写"能不能调、边界在哪"（prompt 契约 §一）。
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": TOOL_RUN_ANALYSIS,
            "description": (
                "把一个热点跑一遍完整分析：拆解爆点要素 → 在创作者本地的素材库里检索可蹭的素材 → "
                "给出命中理由与覆盖缺口 → 写一版文案初稿。用户说「蹭一下这个热点」「看看有没有"
                "素材」「写一版文案」时调用它。用户只发了图（1–9 张，没写热点文字）时，热点参数"
                "留空即可，工具会用那组图自己解析出描述（多张合并成一份）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hotspot": {
                        "type": "string",
                        "description": "热点原文（1–500 字）。整理用户原话即可，不要改写成标题。"
                        "只有图片时留空。",
                },
                    "topk": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "每个热点返回几条候选素材，默认 5。用户要「多看几条」时再改。",
                },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_PARSE_IMAGE,
            "description": (
                "把用户这一轮发来的图拆成「热点描述 + 爆点要素」。用户一次可能发 1–9 张，"
                "**整组当一条热点**一次解析（多张会合并成一份描述与一份要素，各自的画面在要素的"
                "evidence 里标了图号）。**只解析图片**：不检索素材、不写文案、不落库、不产生运行"
                "记录。用户只丢图、想先看看图里是什么热点，或者你想先确认图片内容再决定下一步时用它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "force_reparse": {
                        "type": "boolean",
                        "description": "用户**明确要求重新解析**这张/这组图时传 true：真正再调用一次"
                        "视觉模型并建立新的原始版本；留空则复用选定版本或有效缓存。",
                    },
                    "clue_version_id": {
                        "type": "string",
                        "description": "可选：要使用的已选定线索版本 id。只在用户明确指定某一版时传；"
                        "留空使用当前选定版本。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_SEARCH_MATERIALS,
            "description": (
                "在创作者本地的素材库里检索能蹭这条热点的素材，返回爆点要素、要素覆盖率与候选"
                "素材（含命中理由与建议用法）。**只检索、不写文案、不产生运行记录**；用户问"
                "「有没有能用的素材」「这条热点我能蹭吗」时用它。热点原文由你整理后传入。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hotspot": {
                        "type": "string",
                        "description": "热点原文（1–500 字）。整理用户原话即可，不要改写成标题。",
                },
                    "topk": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "返回几条候选素材，默认 5。用户要「多看几条」时再改。",
                },
                },
                "required": ["hotspot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_READ_RUN,
            "description": (
                "读回一次**已完成**的运行结果（按 run_id）：每条热点的爆点要素、要素覆盖率、"
                "候选素材的**完整清单**（rank / 标题 / 分数 / 用途摘述，最多 8 条）、素材总数与"
                "初稿标题。它不重新分析、不检索、不花钱；用户追问「刚才那次」「上次那批」的结果"
                "细节，或用「第 4 条」这样指代候选素材时用它——**序号就是这里返回的 rank**，"
                "别处拿不到同一份编号。**run_id 可以不传**——不传就用"
                "这个会话里最近一次运行的结果（用户说「上次那批」时正是这种情况）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "要读回的运行 id（uuid）。可取之前分析的返回值；"
                        "用户指的是「上次那批」时留空即可，不要自己编。",
                },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_PREVIEW_COVERAGE,
            "description": (
                "只做要素拆解与覆盖估算：把一条热点拆成九类爆点要素，再按你本地素材库里"
                "「已经沾边的素材」估算要素覆盖率、指出缺口并给出补拍建议。**这一步不产出候选"
                "素材、不写文案、不产生运行记录**；用户想先看看这条热点由什么构成、缺哪些画面，"
                "或者想省一次完整分析的成本时用它。要看具体候选素材请改用 search_materials，"
                "要完整分析和文案请用 run_hotspot_analysis。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hotspot": {
                        "type": "string",
                        "description": "热点原文（1–500 字）。整理用户原话即可，不要改写成标题。",
                },
                },
                "required": ["hotspot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_REVISE_DRAFT,
            "description": (
                "按用户的要求改写**某次运行已经产出的文案初稿**（改结构、口吻、详略、角度）："
                "不重新分析、不检索素材、**不覆盖原结果**——改后的文案只出现在这次回复里，"
                "由你讲给用户。用户在初稿基础上提修改意见（「标题再狠一点」「正文写短些」"
                "「换成避坑角度」）时用它。要求越界（要写他人姓名/肖像、要编造素材里没有的事）时"
                "工具会拒绝并给出理由，你把这个理由如实讲给用户，不要替他换个说法再来一次。"
                "**run_id 可以不传**：不传就改这个会话里最近一次运行产出的初稿"
                "（用户说「刚才那版」时正是这种情况）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "要改写的那次运行的 run_id（uuid）。取自之前分析的"
                        "返回值；用户指的是「刚才那版」时留空即可，不要自己编。",
                },
                    "request": {
                        "type": "string",
                        "description": "用户的修改要求（1–500 字）。整理用户原话即可，"
                        "不要替他加要求。",
                },
                    "hotspot_index": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "改第几条热点的初稿，默认 1。",
                },
                    "base_version_id": {
                        "type": "string",
                        "description": "可选：以哪个版本为基础改（版本 id）。用户明确说「回到第一版"
                        "再改」时传它；留空则用该对象**最近成功版本**（不是运行初稿）。",
                },
                },
                "required": ["request"],
            },
        },
    },
]


# 网页工具的 function schema（PR2）：**默认不下发**，由 `tools_for(cfg)` 按 `[web].enabled` /
# `fetch_enabled` 追加。放在 `TOOLS` 外面，是因为 `TOOLS` 就是「关闭态下发的清单」。
# description 就是 prompt：模型靠它决定该不该调、越界时怎么拒。
TOOL_SCHEMA_SEARCH_HOTSPOTS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_SEARCH_HOTSPOTS,
        "description": (
            "按方向搜当天的热点候选（返回标题 / 链接 / 摘要，不打开页面）。用户说「帮我找找最近"
            "有什么能蹭的热点」「这个方向最近在聊什么」时调用它。只用于发现**可以蹭**的热点；"
            "不用于天气、新闻、百科、股价等通用信息查询。搜索结果只是候选、不是事实：要写进"
            "正文的标题与数字，得再用读网页的工具核过。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索用的一句话（1–80 字）：用户想找什么方向的热点。整理用户"
                    "原话即可，不要堆关键词，也不要自己编时间范围。",
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "返回几条候选（1–10），默认 5。用户要「多看几条」时再改。",
                },
            },
            "required": ["query"],
        },
    },
}

TOOL_SCHEMA_FETCH_URL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_FETCH_URL,
        "description": (
            "打开一个网页，取回正文（去掉导航 / 广告，超长会截断）。搜索摘要不足以判断、或用户"
            "贴了链接想让你看内容时调用它。只读取搜索结果里给出的链接，或用户直接贴出的链接；"
            "不要自己拼 URL。内容被截断时用 `start_index` 接着读。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要读的网页地址：必须是 `http://` 或 `https://` 开头的完整"
                    "链接，从搜索结果或用户消息里原样取，不要拼接。",
                },
                "start_index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "从正文第几个字符开始读，默认 0。上一段结果标了截断"
                    "（truncated）时，用它接着读后面那段。",
                },
            },
            "required": ["url"],
        },
    },
}


class ProgressFn(Protocol):
    """工具进度回调：`(中文标签, done, total)`；R3 起可带结构化 `update`。

    `update` 是 `runs.stage` 快照的投影（阶段键 / 真实状态 / 源尝试号 / 重试次数 / 错误类别 /
    `run_id` / `job_id`）——对话侧据此合并出忠实区分 running / failed / skipped / 重试的快照。
    只收三个位置参数的旧实现仍然可用（不传 `update` 时行为与以前一致）。
    """

    async def __call__(
        self,
        stage: str,
        done: int = 0,
        total: int = 0,
        *,
        update: dict[str, Any] | None = None,
    ) -> None: ...


EnqueueFn = Callable[[str], Awaitable[None]]
ToolHandler = Callable[..., Awaitable["ToolOutcome"]]


@dataclass(frozen=True)
class ClueRecord:
    """一次热点拆解的结果 + 它的 prompt 版本与这次的用量。"""

    clue: HotspotClue
    version: int
    cost_cny: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # R5（E7OBS2-04）：用量来源口径——actual / estimated / cached（本轮缓存命中）。
    usage_status: str = ""


@dataclass
class ToolCache:
    """一轮对话内共享的中间结果：同一件事不重复付费、不重复等模型（S7.7）。"""

    clues: dict[str, ClueRecord] = field(default_factory=dict)
    image_clues: dict[str, tuple[ImageClueParse, int]] = field(default_factory=dict)
    image_generations: dict[str, str] = field(default_factory=dict)
    # 网页两件（PR2）：命中缓存不重复请求、也不计调用次数（消费方见 Task 6 的 handler）
    searches: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # 键：query
    pages: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)  # 键：(url, start_index)
    seen_urls: set[str] = field(default_factory=set)  # 本轮搜到的 url：区分「用户点名」与「自主调用」
    robots: dict[str, Any] = field(default_factory=dict)  # 键：host（robots 解析器）
    web_calls: int = 0  # 本轮网页工具调用计数（缓存命中不算）


@dataclass
class ToolContext:
    """跑工具需要的一切（由对话运行器注入；离线测试直接给假实现）。"""

    cfg: AppConfig
    session: AsyncSession
    session_id: uuid.UUID | None = None
    enqueue: EnqueueFn | None = None
    http: httpx.AsyncClient | None = None
    caller: StructuredCaller | None = None
    cache: ToolCache = field(default_factory=ToolCache)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    poll_interval_s: float = 1.5
    max_wait_s: float | None = None  # None = 取 [queue].task_time_limit_s
    # 本会话最近**一组**图片附件（跨轮图片回落，S7.10 单张 → S7.16 整组）；
    # 运行器注入，离线测试直接给假实现。空列表 = 本会话还没有图。
    recent_attachments: list[dict[str, Any]] = field(default_factory=list)
    # 同步检查点由轮次运行器装配；工具层只提交已完成子步骤的事实。
    checkpoint: Callable[[dict[str, Any]], None] | None = None
    checkpoint_scope: str = ""
    # E7 第六批（S7.27）：本轮标识（缓存消费 / 观测的关联键）
    turn_id: str = ""
    # v2 修复（E7CTX-07）：本轮来源用户消息（认领的 source_message_id）；空 = 未提供
    source_message_id: str = ""
    # A04：随来源用户消息**持久化**的可信操作绑定（服务端校验过）；空 = 本轮没有绑定。
    # 消费者只读这一份，不采信模型在工具参数里重新生成的绑定。
    operation_binding: dict[str, Any] | None = None
    # v2 修复（E7CTX-15）：轮次上下文策略——模块只问策略，不自行读配置
    policy: Any = None


@dataclass
class ToolOutcome:
    """一次工具调用的结果：喂回模型 + 落 `chat_messages.tool_calls`。"""

    status: str  # succeeded / failed
    payload: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    # 这个工具内部用掉的 prompt 版本（如 `{"hotspot_clue": 1}`），由对话图汇总进
    # `chat_messages.prompt_versions`——不写进 payload，所以不会漏给模型看。
    prompt_versions: dict[str, int] = field(default_factory=dict)

    def as_tool_message(self) -> str:
        """给模型的 tool 消息正文（JSON 字符串，模型自己读字段）。"""
        import json

        body: dict[str, Any] = {"status": self.status, **self.payload}
        if self.error:
            body["error"] = self.error
        return json.dumps(body, ensure_ascii=False)


def _checkpoint(ctx: ToolContext, component: str, **facts: Any) -> None:
    """在下一次可取消 await 前登记已完成的工具内部子步骤。"""
    if ctx.checkpoint is None:
        return
    ctx.checkpoint({"scope": ctx.checkpoint_scope, "component": component, **facts})


def _step(
    steps: list[dict[str, Any]], stage: str, done: int = 0, total: int = 0
) -> None:
    steps.append(
        {
            "stage": stage,
            "done": done,
            "total": total,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
    )


def _reason(exc: Exception) -> str:
    """异常 → 一句能直接回给用户的原因（ApiError 有写好的 message）。"""
    return public_error_message(exc)


def _records_cost(caller: StructuredCaller, before: int) -> float:
    """取 `StructuredCaller` 这次调用新增的成本（records 的增量）。"""
    return round(
        sum(float(item.get("cost_cny") or 0.0) for item in caller.records[before:]), 4
    )


def _records_tokens(caller: StructuredCaller, before: int) -> tuple[int, int]:
    """取 `StructuredCaller` 这次调用新增的 token 用量（records 的增量）。"""
    window = caller.records[before:]
    return (
        sum(int(item.get("prompt_tokens") or 0) for item in window),
        sum(int(item.get("completion_tokens") or 0) for item in window),
    )


def _records_usage_status(caller: StructuredCaller, before: int) -> str:
    """取 `StructuredCaller` 这次调用新增记录的**用量来源**（R5 / E7OBS2-04）。

    一段窗口可能包含多次调用，按事实汇总（E7OBS4-05）：
    - **缺来源（空串）按 unknown**——模型记录存在但口径缺失，不能被同窗口的 actual 掩盖
      （数值即使全零也是调用事实）；`actual + estimated` 汇总为 estimated，不冒充 actual；
    - 只有全窗口都是 cached 才是 cached（缓存与真实调用混合时按真实调用集合汇总）；
    - 窗口为空（如调用失败没留下记录）返回空串，由调用方按场景处理。
    """
    statuses = [str(item.get("usage_status") or "") for item in caller.records[before:]]
    if not statuses:
        return ""
    if any(status in ("", "unknown") for status in statuses):
        return "unknown"
    if "estimated" in statuses:
        return "estimated"
    if all(status == "cached" for status in statuses):
        return "cached"
    return "actual"


def _element_view(element: Element) -> dict[str, Any]:
    """要素 → 回给模型的精简形状（带中文标签，省得模型自己翻译）。"""
    result = {
        "type": element.type,
        "label": TYPE_LABELS.get(element.type, element.type),
        "value": element.value,
        "weight": round(float(element.weight), 3),
        "confidence": round(float(element.confidence), 3),
        "evidence": element.evidence,
    }
    return result


def _elements_of(clue: dict[str, Any] | HotspotClue) -> list[dict[str, Any]]:
    raw = clue.to_dict() if isinstance(clue, HotspotClue) else dict(clue or {})
    return [
        _element_view(Element.from_dict(item)) for item in (raw.get("elements") or [])
    ]


def _candidate_view(candidate: Any) -> dict[str, Any]:
    """候选 → 回给模型的精简形状（不塞整份素材对象，省上下文）。"""
    material = candidate.material
    title = material.title or Path(str(material.path or "")).name
    result = {
        "rank": candidate.rank,
        "score": round(float(candidate.score), 3),
        "material_id": str(candidate.material_id),
        "title": title,
        "type": material.type,
        "duration_s": round(float(material.duration_s or 0), 1),
        "tags": [str(tag) for tag in (material.tags or [])][:TAG_PREVIEW_LIMIT],
        "hits": [
            {"element_type": hit.element_type, "clue_value": hit.clue_value}
            for hit in (candidate.hits or [])
        ][:HIT_PREVIEW_LIMIT],
        "usage": candidate.usage,
    }
    return result


def _candidate_brief(candidate: dict[str, Any]) -> dict[str, Any]:
    """`load_run` 的候选字典 → 给模型看的**一行**（P0：让「第 N 条」有据可依）。

    与 `_candidate_view` 同一套键名，只是不带 tags / hits 这类重字段：`read_run` 一次要给
    每条热点的整份候选清单，逐条塞完整形状会把上下文预算吃光。`rank` 必须原样透出——用户说
    「第 4 条」时的对齐口径只有它，模型不能自己按分数重排。
    """
    material = candidate.get("material") or {}
    title = str(material.get("title") or "")
    if not title:
        title = Path(str(material.get("path") or "")).name
    return {
        "rank": int(candidate.get("rank") or 0),
        "title": title,
        "score": round(float(candidate.get("score") or 0), 3),
        "usage": str(candidate.get("usage") or ""),
        "material_id": str(candidate.get("material_id") or ""),
    }


def _candidate_briefs(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """候选清单 → 精简列表（上限同 `search_materials`，总数另由 `count` 给出）。"""
    return [
        _candidate_brief(item) for item in candidates[:CANDIDATE_PREVIEW_LIMIT]
    ]


def _check_hotspot(args: dict[str, Any]) -> tuple[str, str]:
    """校验 `hotspot` 参数；返回（去掉首尾空白的原文，错误文案）。"""
    text = str(args.get("hotspot") or "").strip()
    if not text:
        return "", "缺少 hotspot：请把用户想蹭的那条热点原文整理后传进来（1–500 字）"
    if len(text) > MAX_HOTSPOT_CHARS:
        return text, (
            f"热点原文超过 {MAX_HOTSPOT_CHARS} 字，请让用户压缩到 "
            f"{MAX_HOTSPOT_CHARS} 字以内再试"
        )
    return text, ""


def _check_topk(raw: Any, default: int) -> tuple[int, str]:
    """校验 `topk` 参数；返回（条数，错误文案）。"""
    if raw is None:
        return int(default), ""
    try:
        topk = int(raw)
    except (TypeError, ValueError):
        return 0, f"topk 必须是 1–{MAX_TOPK} 的整数，收到 {raw!r}"
    if not 1 <= topk <= MAX_TOPK:
        return 0, f"topk 必须在 1–{MAX_TOPK} 之间，收到 {topk}"
    return topk, ""


def _check_query(args: dict[str, Any]) -> tuple[str, str]:
    """校验 `query` 参数；返回（去掉首尾空白的搜索方向，错误文案）。"""
    text = str(args.get("query") or "").strip()
    if not text:
        return "", (
            "缺少 query：请把用户想找的热点方向整理成一句话传进来（"
            f"1–{MAX_QUERY_CHARS} 字）"
        )
    if len(text) > MAX_QUERY_CHARS:
        return text, (
            f"query 超过 {MAX_QUERY_CHARS} 字，请让用户压缩到 {MAX_QUERY_CHARS} 字以内再试"
        )
    return text, ""


def _check_count(raw: Any) -> tuple[int, str]:
    """校验 `count` 参数；返回（条数，错误文案）。缺省 = `DEFAULT_SEARCH_COUNT`。"""
    if raw is None:
        return DEFAULT_SEARCH_COUNT, ""
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return 0, f"count 必须是 1–{MAX_SEARCH_COUNT} 的整数，收到 {raw!r}"
    if not 1 <= count <= MAX_SEARCH_COUNT:
        return 0, f"count 必须在 1–{MAX_SEARCH_COUNT} 之间，收到 {count}"
    return count, ""


def _check_url(args: dict[str, Any]) -> tuple[str, str]:
    """校验 `url` 参数；返回（原样地址，错误文案）。

    只放行 http(s)：`file://` / `javascript:` 之类在进入适配器前就拒掉，
    文案要告诉模型「从哪拿链接」，不是「参数错误」。
    """
    text = str(args.get("url") or "").strip()
    if not text:
        return "", "缺少 url：请把要读的网页地址完整传进来（http:// 或 https:// 开头）"
    hint = "请从搜索结果或用户消息里原样取链接，不要自己拼"
    try:
        parsed = urlparse(text)
    except ValueError:
        return "", f"url 不是合法的网页地址，收到 {text[:80]!r}；{hint}"
    if parsed.scheme.lower() not in WEB_URL_SCHEMES:
        return "", f"url 必须以 http:// 或 https:// 开头，收到 {text[:80]!r}；{hint}"
    if not parsed.hostname:
        return "", f"url 里没有主机名，收到 {text[:80]!r}；{hint}"
    return text, ""


def _check_start_index(raw: Any) -> tuple[int, str]:
    """校验 `start_index` 参数；返回（起点，错误文案）。缺省 = 从头读。"""
    if raw is None:
        return 0, ""
    try:
        start = int(raw)
    except (TypeError, ValueError):
        return 0, f"start_index 必须是 0 或正整数，收到 {raw!r}"
    if start < 0:
        return 0, (
            f"start_index 不能是负数（收到 {start}）：要从正文开头读就传 0，或者干脆不传"
        )
    return start, ""


async def _clue_for(ctx: ToolContext, text: str) -> ClueRecord:
    """拆解热点（同一轮内同一段文本只拆一次）；缓存命中时本次成本记 0、口径标 `cached`。"""
    cached = ctx.cache.clues.get(text)
    if cached is not None:
        # R5（E7OBS2-04）：缓存命中 = 本轮无新增模型消费——与"免费真实请求（actual 零值）"区分
        return replace(
            cached,
            cost_cny=0.0,
            prompt_tokens=0,
            completion_tokens=0,
            usage_status="cached",
        )
    if ctx.caller is None:  # pragma: no cover - 运行器总会注入
        raise LLMError("对话工具缺少文本模型 caller")
    before = len(ctx.caller.records)
    outcome = await extract_clue(text, ctx.caller)
    prompt_tokens, completion_tokens = _records_tokens(ctx.caller, before)
    record = ClueRecord(
        clue=outcome.value,
        version=int(outcome.version or 1),
        cost_cny=_records_cost(ctx.caller, before),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        usage_status=_records_usage_status(ctx.caller, before),
    )
    ctx.cache.clues[text] = record
    _checkpoint(
        ctx,
        "clue",
        cost_cny=record.cost_cny,
        prompt_tokens=record.prompt_tokens,
        completion_tokens=record.completion_tokens,
        usage_status=record.usage_status,
        prompt_versions={CLUE_TASK: record.version},
    )
    return record


async def _run_belongs_to_session(ctx: ToolContext, run_id: str) -> bool:
    """run 是否属于当前会话（A04：跨会话引用一律当作不存在）。

    归属以"会话里真实出现过的消息 run_id"为准（工具结果的 run_id 由 analyses 落消息时写入）；
    离线 / 无会话上下文（session_id 为空）不做归属校验，保持旧测试路径可用。
    """
    if ctx.session is None or ctx.session_id is None:
        return True
    try:
        run_uuid = uuid.UUID(str(run_id))
    except (TypeError, ValueError):
        return False
    count = int(
        (
            await ctx.session.execute(
                select(func.count())
                .select_from(ChatMessage)
                .where(
                    ChatMessage.session_id == ctx.session_id,
                    ChatMessage.run_id == run_uuid,
                )
            )
        ).scalar_one()
    )
    return count > 0


async def _resolve_run_id(ctx: ToolContext, raw: str) -> tuple[str, str]:
    """把 `run_id` 参数落成一个真 uuid；**不传就用本会话最近一次运行产出的 id**。

    返回（run_id，错误文案）。多轮对话里模型看不到工具返回值里的 id（历史只装配消息正文），
    所以"上次那批""刚才那版"必须由服务端兜底解析——否则 `read_run` / `revise_draft` 只能靠
    用户手打 uuid 才用得起来（S7.9 冒烟实测过这个坑）。
    """
    text = raw.strip()
    if text:
        try:
            uuid.UUID(text)
        except (TypeError, ValueError):
            return "", f"run_id 必须是一个 uuid，收到 {text!r}"
        return text, ""
    if ctx.session_id is None:
        return "", "没传 run_id，也拿不到会话上下文；请把要读/要改的 run_id 带上"
    found = await latest_run_id(ctx.session, ctx.session_id)
    if not found:
        return "", (
            "这个会话还没跑过分析——先让用户跑一次完整分析，才有结果可读、有初稿可改"
        )
    return found, ""


def _image_group(
    attachments: list[dict[str, Any]] | None, ctx: ToolContext
) -> list[dict[str, Any]]:
    """这一轮要用的图片组：**本轮带的优先**，本轮没带才回落到本会话最近一组（S7.16）。

    单张与多张走同一条路（1 张就是只有一项的组），调用方不再关心数量。
    """
    group = [item for item in (attachments or []) if item]
    return group or list(ctx.recent_attachments or [])


async def _bound_image_group(
    ctx: ToolContext, args: dict[str, Any], attachments: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """显式旧线索绑定按其图片组取原附件，不能改用最近另一组图片。"""
    group = _image_group(attachments, ctx)
    binding = dict(ctx.operation_binding or {})
    key = str(binding.get("object_key") or "")
    version_id = str(binding.get("clue_version_id") or args.get("clue_version_id") or "")
    if ctx.session is None or ctx.session_id is None:
        return group
    if version_id:
        try:
            version = await artifact_versions.get_version(ctx.session, ctx.session_id, uuid.UUID(version_id))
        except ValueError as exc:
            raise BadRequestError("线索版本标识不合法") from exc
        if version is None or version.object_type != "clue" or version.status != "succeeded":
            raise ConflictError("选定线索不属于当前会话", {"reason": "artifact_version_mismatch"})
        if key and key != version.object_key:
            raise ConflictError("线索版本与对象不一致", {"reason": "artifact_version_mismatch"})
        key = str(version.object_key)
    if not key.startswith("clue:images:"):
        return group
    target = key.removeprefix("clue:images:")

    def matches(items: list[dict[str, Any]]) -> bool:
        digests = [str(item.get("sha256") or "") for item in items]
        return bool(items) and all(digests) and image_cache.group_digest(digests) == target

    if matches(group):
        return group
    if attachments:
        raise ConflictError("本轮图片与绑定的线索不是同一组", {"reason": "artifact_version_mismatch"})
    rows = (await ctx.session.execute(select(ChatMessage.attachments).where(
        ChatMessage.session_id == ctx.session_id, ChatMessage.role == "user",
        func.jsonb_array_length(ChatMessage.attachments) > 0,
    ).order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()))).scalars().all()
    for items in rows:
        candidate = [dict(item) for item in items if item.get("kind") == "image"]
        if matches(candidate):
            return candidate
    raise ConflictError("这版线索的原图片已不可用，不能替换成另一组图片", {
        "reason": "artifact_version_mismatch", "retryable": False,
    })


def _image_binding(
    ctx: ToolContext, args: dict[str, Any]
) -> tuple[bool, str, str]:
    """解析图片入口的**可信绑定**（A04 / A08）：返回 `(force_reparse, clue_version_id, 冲突原因)`。

    绑定来自来源用户消息（服务端已校验并持久化）；模型参数只能在其未给定时补充，
    显式冲突（不同版本 / 强制重解析夹带旧版本）一律拒绝，不覆盖用户选择。
    """
    binding = dict(ctx.operation_binding or {})
    operation = str(binding.get("operation") or "")
    force = bool(args.get("force_reparse") or False)
    clue = str(args.get("clue_version_id") or "")
    if operation in ("parse_clue_from_image", "run_hotspot_analysis"):
        force = force or bool(binding.get("force_reparse") or False)
        bound = str(binding.get("clue_version_id") or "")
        if clue and bound and clue != bound:
            return force, clue, "artifact_version_mismatch"
        clue = bound or clue
    if force and clue:
        return force, clue, "force_reparse_conflicts_with_version"
    return force, clue, ""


async def _constraints_for(
    ctx: ToolContext, *, object_key: str = ""
) -> list[dict[str, Any]]:
    """本轮**适用事实快照**（A05）：同一份口径交给改稿 / 分析投递与 worker。

    只含 session + 当前话题 + 服务端已验证对象（含本次操作的产物键）的生效事实；
    失效事实、其他话题与其他对象的约束都不混入。没有会话上下文（离线）时返回空列表。
    """
    if ctx.session is None or ctx.session_id is None:
        return []
    if ctx.policy is not None and not ctx.policy.enforce:
        return []
    stored = await context_state.load(ctx.session, ctx.session_id)
    if stored is None:
        return []
    keys = {object_key} if object_key else set()
    facts = context_rules.applicable_facts(
        stored.snapshot, topic_id=stored.topic_id, object_keys=keys
    )
    return context_rules.constraint_view(facts)


async def _cache_bypass(ctx: ToolContext, identity: dict[str, Any] | None,
                        request_id: str, reason: str) -> None:
    if identity is None or ctx.session is None or ctx.session_id is None:
        return
    await record_observation(ctx.session, event_type="cache_lookup_finished",
        session_id=ctx.session_id, key=request_id, turn_id=ctx.turn_id, call_id=request_id,
        payload={"outcome": "bypass", "level": "none", "reason": reason,
                 "cache_key": image_cache.cache_key(identity)})


async def _remember_image(ctx: ToolContext, key: str, parsed: ImageClueParse,
                          identity: dict[str, Any] | None) -> None:
    ctx.cache.image_clues[key] = (parsed, _prompt_version_of(parsed))
    generation = str(uuid.uuid4())
    ctx.cache.image_generations[key] = generation
    if ctx.session is not None and ctx.session_id is not None and identity is not None:
        await record_observation(ctx.session, event_type="cache_entry_created",
            session_id=ctx.session_id, key=f"turn:{generation}", turn_id=ctx.turn_id,
            model=str(identity.get("model") or ""),
            prompt_version=f"image_cache:v{identity.get('prompt_version')}",
            policy_version=str(ctx.policy.mode if ctx.policy else "enforce"),
            payload={"generation_id": generation, "cache_key": key, "cache_layer": "turn"})


async def _consume_image(ctx: ToolContext, parsed: ImageClueParse) -> None:
    if ctx.session is None or ctx.session_id is None or parsed.cache_status != "hit" \
            or not parsed.cache_request_id:
        return
    await image_cache.consume(ctx.session, session_id=ctx.session_id,
        lookup_result=image_cache.CacheLookup(
            outcome="hit", level=parsed.cache_level, key=parsed.cache_key,
            generation_id=parsed.generation_id, cross_turn=parsed.cross_turn),
        turn_id=ctx.turn_id, call_id=parsed.cache_request_id)


async def _image_clue_for(
    ctx: ToolContext,
    attachments: list[dict[str, Any]],
    *,
    force_reparse: bool = False,
    clue_version_id: str = "",
) -> ImageClueParse:
    """解析一组图片：选定编辑版 → 轮内 → 持久层 → 真实调用（v2 §2.5）。

    四层顺序与口径：

    1. **选定编辑版**（E7CTX-12 / A08）：显式选定或绑定版本优先于任何缓存；命中旧视觉缓存
       不得覆盖编辑版，也不因编辑而无故重跑视觉模型。**没有显式选择时不拿对象 head 冒充
       用户选择**——原始解析的复用走下一层 L1/L2 的完整身份与 TTL；
    2. **轮内缓存**（既有安全能力，off 也保留）：键是内容身份，命中前同样过附件校验；
    3. **持久层**（仅 enforce）：命中前校验归属与存在性；`force_reparse` 显式绕过轮内、
       选定版与持久缓存，真正调用一次并建立**新的原始业务版本**；
    4. **真实调用**：成功后登记原始 clue 版本并写缓存（写失败不阻断本次解析）。
    """
    policy = ctx.policy
    group = [item for item in attachments if item]
    # 路径键只用于"无法计算内容身份"的离线兜底；生产路径用完整内容身份（见下），
    # 同路径改写 / 图片顺序变化 / 模型或 prompt 版本变化都不会命中旧结果（A07）。
    path_key = "|".join(str(item.get("path") or "") for item in group)
    enforce = policy is None or policy.enforce   # 离线路径按旧行为处理

    # ---- 附件内容身份（E7CTX-10：L1 命中前也要过校验）----
    # 离线 / 非会话路径（session=None）无法做归属校验：保持旧行为（轮内复用不校验），
    # 生产轮次一定带 session_id，校验按 ensure_digests 三道口径执行。
    identity: dict[str, Any] | None = None
    digests: list[str] = []
    digest_ms = 0
    attachments_valid = ctx.session_id is None    # None = 无法校验（离线），不视为失效
    if ctx.session_id is not None:
        digests, digest_ms, failures = await image_cache.ensure_digests(
            ctx.cfg, str(ctx.session_id), group
        )
        if failures or not all(digests):
            logger.warning("附件校验失败（删除 / 越界 / 内容变化）：%s", failures)
            attachments_valid = False
        else:
            # A07：摘要 / 归属 / 存在性全部通过后才允许命中 L1；此前漏了这一行，
            # 真实会话的有效图片被当成失效，每轮都新增一次视觉调用。
            attachments_valid = True
            identity = image_cache.build_identity(ctx.cfg, digests)

    # 轮内键 = 有序内容摘要 + 模型 / 参数 / prompt / schema 身份（A07）；
    # 没有内容身份（离线 / 校验失败）时才退回路径键。
    key = image_cache.cache_key(identity) if identity is not None else path_key
    cache_request_id = str(uuid.uuid4())
    persistent = bool(enforce and ctx.policy is not None and ctx.policy.persistent_cache)
    layer = "turn" if key in ctx.cache.image_clues and not force_reparse else (
        "persistent" if persistent and not force_reparse else "none")
    if identity is not None and ctx.session is not None and ctx.session_id is not None:
        await record_observation(ctx.session, event_type="cache_request_started",
            session_id=ctx.session_id, key=cache_request_id, turn_id=ctx.turn_id,
            call_id=cache_request_id, model=str(identity.get("model") or ""),
            prompt_version=f"image_cache:v{identity.get('prompt_version')}",
            policy_version=str(ctx.policy.mode if ctx.policy else "enforce"),
            payload={"cache_key": key, "cache_layer": layer,
                     "tool_call_id": ctx.checkpoint_scope})

    # ---- 选定编辑版优先（A08）：显式选择先于任何缓存 ----
    group_digest_value = image_cache.group_digest(digests)
    bound_key = str((ctx.operation_binding or {}).get("object_key") or "")
    if bound_key.startswith("clue:images:") and bound_key != f"clue:images:{group_digest_value}":
        raise ConflictError("原图片内容已变化，与绑定线索不一致", {
            "reason": "artifact_version_mismatch", "retryable": False})
    if identity is not None and not force_reparse and enforce and ctx.session_id is not None:
        selected = await _selected_clue_parse(
            ctx, group_digest_value, clue_version_id=clue_version_id
        )
        if selected is not None:
            logger.info("使用用户选定 / 编辑的线索版本（不重跑视觉模型）")
            await _cache_bypass(ctx, identity, cache_request_id, "selected_clue")
            _checkpoint(ctx, "image_parse", cost_cny=0.0, prompt_tokens=0,
                        completion_tokens=0, usage_status="cached",
                        cache_level="selected_edit",
                        version_id=selected.version_id,
                        prompt_versions=dict(selected.prompt_versions))
            return selected

    cached = ctx.cache.image_clues.get(key) if key else None
    if cached is not None and not force_reparse:
        if not attachments_valid:
            # 附件已失效：轮内结果不能复用（E7CTX-10），按 miss 重新走
            ctx.cache.image_clues.pop(key, None)
        else:
            parsed, _version = cached
            logger.info("图片线索缓存命中：%d 张（不重复计费）", len(group))
            if ctx.session_id is not None and ctx.session is not None:
                await image_cache.record_turn_hit(
                    ctx.session,
                    session_id=ctx.session_id,
                    key=key or str(len(group)),
                    turn_id=ctx.turn_id,
                    call_id=cache_request_id,
                    images=len(group),
                    generation_id=ctx.cache.image_generations.get(key, ""),
                )
            cached_result = replace(
                parsed,
                cost_cny=0.0,
                prompt_tokens=0,
                completion_tokens=0,
                usage_status="cached",
                cache_status="hit",
                cache_level="turn",
                generation_id=ctx.cache.image_generations.get(key, ""),
                cache_key=key,
                cache_request_id=cache_request_id,
            )
            _checkpoint(ctx, "image_parse", cost_cny=0.0, prompt_tokens=0,
                        completion_tokens=0, usage_status="cached",
                        prompt_versions=dict(cached_result.prompt_versions))
            return cached_result

    # ---- 持久层（仅 enforce；force_reparse 绕过）----
    lookup = None
    if identity is not None and not force_reparse and enforce \
            and ctx.session_id is not None and policy is not None \
            and policy.persistent_cache:
        lookup = await image_cache.lookup(
            ctx.session,
            ctx.cfg,
            session_id=ctx.session_id,
            identity=identity,
            turn_id=ctx.turn_id,
            call_id=cache_request_id,
        )
        if lookup.hit and lookup.payload:
            cached_parsed = _parse_cached_clue(ctx.cfg, lookup.payload)
            if cached_parsed is not None:
                logger.info("图片解析持久缓存命中：%d 张（跨轮=%s）",
                            len(group), lookup.cross_turn)
                cached_parsed = replace(cached_parsed, cache_key=key,
                    generation_id=lookup.generation_id, cache_request_id=cache_request_id,
                    cross_turn=lookup.cross_turn)
                await _remember_image(
                    ctx, key, cached_parsed, identity
                )
                _checkpoint(ctx, "image_parse", cost_cny=0.0, prompt_tokens=0,
                            completion_tokens=0, usage_status="cached",
                            cache_level=lookup.level, digest_ms=digest_ms,
                            generation_id=lookup.generation_id,
                            prompt_versions=dict(cached_parsed.prompt_versions))
                return cached_parsed
            logger.warning("图片缓存条目的结构无法解析，按失效处理")

    if lookup is None:
        await _cache_bypass(ctx, identity, cache_request_id,
                            "explicit_reparse" if force_reparse else "policy_disabled")

    # ---- 真实调用 ----
    images = [await _read_image(ctx, item) for item in group]
    if len(images) == 1:
        data, mime = images[0]
        parsed = await parse_image_clue(ctx.cfg, data, mime, http=ctx.http)
    else:
        parsed = await parse_image_clue_multi(ctx.cfg, images, http=ctx.http)
    stored_result: ChatArtifactVersion | None = None
    if identity is not None and ctx.session_id is not None and enforce:
        # A08：显式重解析建立**新的**原始业务版本；普通首次解析保持幂等（不重复建版本）。
        stored_result = await _register_original_clue(
            ctx, group_digest_value, parsed, force_reparse=force_reparse,
            identity=identity,
        )
        parsed = replace(
            parsed,
            version_id=str(stored_result.id) if stored_result is not None else "",
            version_no=int(stored_result.version_no or 0) if stored_result is not None else 0,
        )
    if key:
        await _remember_image(ctx, key, parsed, identity)
    stored: bool | None = None
    if (
        identity is not None
        and ctx.session_id is not None
        and enforce
        and (policy is None or policy.persistent_cache)
    ):
        stored = await image_cache.store(
            ctx.session,
            ctx.cfg,
            session_id=ctx.session_id,
            identity=identity,
            payload={
                "clue": parsed.clue.to_dict(),
                "raw_text": parsed.raw_text,
                "prompt_versions": dict(parsed.prompt_versions),
                "version_id": parsed.version_id,
                "version_no": parsed.version_no,
            },
            turn_id=ctx.turn_id,
            message_id=str((group[0] or {}).get("message_id") or ""),
            order=len(group),
            force_rebuild=force_reparse,
        )
    _checkpoint(
        ctx,
        "image_parse",
        cost_cny=float(parsed.cost_cny or 0),
        prompt_tokens=int(parsed.prompt_tokens),
        completion_tokens=int(parsed.completion_tokens),
        usage_status=str(parsed.usage_status or "estimated"),
        prompt_versions=dict(parsed.prompt_versions),
        cache_level=(lookup.level if lookup else "none"),
        cache_store=("stored" if stored else "failed") if stored is not None else "off",
        force_reparse=force_reparse,
        version_id=parsed.version_id,
    )
    if stored is not None:
        parsed = replace(
            parsed,
            cache_status="miss" if stored else "error",
            cache_level="persistent",
        )
    elif force_reparse:
        parsed = replace(parsed, cache_status="bypass", cache_level="none")
    return parsed


async def _selected_clue_parse(
    ctx: ToolContext, group_digest_value: str, *, clue_version_id: str = ""
) -> ImageClueParse | None:
    """读取用户**显式选定 / 编辑**的线索版本（E7CTX-12 / A08）；没有显式选择返回 None。

    没有显式选择时**不读对象 head**——head 只能表示"最近一次成功解析"，不代表用户的
    选择；原始解析复用由调用方走 L1 / 持久缓存的完整身份与 TTL。

    显式版本（`object_refs` 里的选定引用或本轮绑定）必须存在、属于本会话、是同一对象键的
    成功 clue 版本；无效 / 失踪时明确报错，不回落 head。
    """
    if ctx.session is None or ctx.session_id is None:
        return None
    object_key = f"clue:images:{group_digest_value}"
    chosen = str(clue_version_id or "")
    if not chosen:
        stored = await context_state.load(ctx.session, ctx.session_id)
        if stored is not None:
            for item in stored.snapshot.object_refs:
                if str(item.get("object_key") or "") == object_key:
                    chosen = str(item.get("version_id") or "")
                    break
    if not chosen:
        return None
    try:
        version_uuid = uuid.UUID(chosen)
    except (TypeError, ValueError) as exc:
        raise BadRequestError("选定的线索版本 id 不是合法 UUID") from exc
    head = await artifact_versions.get_version(ctx.session, ctx.session_id, version_uuid)
    if (
        head is None
        or str(head.object_key) != object_key
        or str(head.object_type) != "clue"
        or str(head.status) != "succeeded"
        or not isinstance(head.content, dict)
    ):
        raise ConflictError(
            "选定的线索版本不存在、已被替换或不属于这组图片，请重新选择",
            {
                "reason": "selected_clue_missing",
                "object_key": object_key,
                "version_id": chosen,
                "retryable": False,
            },
        )
    raw_text = str(head.content.get("raw_text") or "").strip()
    clue_payload = head.content.get("clue")
    if not raw_text or not isinstance(clue_payload, dict):
        raise ConflictError(
            "选定的线索版本内容不完整，无法复用，请重新解析或重新编辑",
            {"reason": "artifact_version_mismatch", "version_id": chosen,
             "retryable": False},
        )
    try:
        clue = HotspotClue.from_dict(clue_payload, hotspot_raw=raw_text)
    except SchemaError as exc:
        raise ConflictError(
            f"选定的线索版本不符合契约：{exc}",
            {"reason": "artifact_version_mismatch", "version_id": chosen,
             "retryable": False},
        ) from exc
    await record_observation(
        ctx.session,
        event_type="artifact_consumed",
        session_id=ctx.session_id,
        key=f"{ctx.turn_id}:{ctx.checkpoint_scope}:{head.id}:selected_clue",
        payload={
            "object_key": str(head.object_key),
            "version_id": str(head.id),
            "version_no": int(head.version_no or 0),
            "cross_turn": True,
            "new_visual_calls": 0,
            "used_for": "selected_clue",
        },
        turn_id=ctx.turn_id,
        call_id=ctx.checkpoint_scope,
        policy_version="selected_clue_v2",
    )
    return ImageClueParse(
        raw_text=raw_text,
        clue=clue,
        prompt_versions={
            str(k): int(v) for k, v in (head.prompt_versions or {}).items()
        },
        cost_cny=0.0,
        prompt_tokens=0,
        completion_tokens=0,
        usage_status="cached",
        cache_status="hit",
        cache_level="selected_edit",
        version_id=str(head.id),
        version_no=int(head.version_no or 0),
    )


async def _register_original_clue(
    ctx: ToolContext,
    group_digest_value: str,
    parsed: ImageClueParse,
    *,
    force_reparse: bool = False,
    identity: dict[str, Any] | None = None,
) -> ChatArtifactVersion | None:
    """登记图片组的原始 clue 版本（E7CTX-12 / A08）。

    每次真实新解析建立内容一致的版本，同轮同调用重放保持幂等。
    不修改已选定的 object_refs——普通后续任务仍消费用户选定的编辑版。
    """
    if ctx.session is None or ctx.session_id is None:
        return None
    object_key = f"clue:images:{group_digest_value}"
    content = {
        "raw_text": parsed.raw_text,
        "clue": parsed.clue.to_dict(),
        "edited_fields": [],
    }
    try:
        return await artifact_versions.register_version(
                ctx.session,
                session_id=ctx.session_id,
                object_key_value=object_key,
                object_type="clue",
                content=content,
                source={
                    "actor": "explicit_reparse" if force_reparse else "image_parse_original",
                    "turn_id": ctx.turn_id,
                    "call_id": ctx.checkpoint_scope,
                    "parse_identity": dict(identity or {}),
                    "message_id": ctx.source_message_id,
                },
                prompt_versions=dict(parsed.prompt_versions),
                request_key_value=artifact_versions.request_key(
                    ctx.session_id, object_key, turn_id=ctx.turn_id,
                    call_id=ctx.checkpoint_scope,
                    request=json.dumps({"identity": identity, "content": content,
                                        "force_reparse": force_reparse},
                                       sort_keys=True, ensure_ascii=False),
                ) if ctx.turn_id and ctx.checkpoint_scope else None,
            )
    except ApiError as exc:  # 登记失败不阻断解析本身
        logger.warning("原始 clue 版本登记失败（不阻断解析）：%s", exc.message)
        return None


def _prompt_version_of(parsed: ImageClueParse) -> int:
    return int(
        next(iter(parsed.prompt_versions.values())) if parsed.prompt_versions else 1
    )


def _parse_cached_clue(cfg: AppConfig, payload: dict[str, Any]) -> ImageClueParse | None:
    """把缓存 payload 还原成解析结果；结构不合法返回 None（按失效处理，不当正常 miss）。"""
    raw_text = str(payload.get("raw_text") or "").strip()
    clue_payload = payload.get("clue")
    if not raw_text or not isinstance(clue_payload, dict):
        return None
    try:
        clue = HotspotClue.from_dict(clue_payload, hotspot_raw=raw_text)
    except SchemaError:
        return None
    _ = cfg
    return ImageClueParse(
        raw_text=raw_text,
        clue=clue,
        prompt_versions={
            str(key): int(value)
            for key, value in (payload.get("prompt_versions") or {}).items()
        },
        cost_cny=0.0,
        prompt_tokens=0,
        completion_tokens=0,
        usage_status="cached",
        cache_status="hit",
        cache_level="persistent",
        version_id=str(payload.get("version_id") or ""),
        version_no=int(payload.get("version_no") or 0),
    )


async def run_hotspot_analysis(
    ctx: ToolContext,
    args: dict[str, Any],
    *,
    attachments: list[dict[str, Any]] | None = None,
    on_progress: ProgressFn | None = None,
) -> ToolOutcome:
    """「跑完整分析」工具：整理热点 → （可选）解析图片 → 提交分析 → 等结果 → 汇总。"""
    steps: list[dict[str, Any]] = []

    async def progress(
        stage: str,
        done: int = 0,
        total: int = 0,
        *,
        update: dict[str, Any] | None = None,
    ) -> None:
        _step(steps, stage, done, total)
        if on_progress is not None:
            # 结构化 `update` 原样透传（R3 / OBS-12）；None 时不带 kwarg，兼容三参旧实现
            if update is None:
                await on_progress(stage, done, total)
            else:
                await on_progress(stage, done, total, update=update)

    # 这里热点可以为空（只发图片时留空），所以不复用 `_check_hotspot` 的"必填"校验
    text = str(args.get("hotspot") or "").strip()
    if len(text) > MAX_HOTSPOT_CHARS:
        return ToolOutcome(
            status="failed",
            steps=steps,
            error=f"热点原文超过 {MAX_HOTSPOT_CHARS} 字，请让用户压缩到 "
            f"{MAX_HOTSPOT_CHARS} 字以内再跑",
        )
    topk, invalid_topk = _check_topk(args.get("topk"), ctx.cfg.match.topk)
    if invalid_topk:
        return ToolOutcome(status="failed", steps=steps, error=invalid_topk)

    versions: dict[str, int] = {}
    clue: dict[str, Any] | None = None
    # 跨轮图片回落（S7.10 / S7.16 按组）：这一轮没带图、用户也没给热点文本时才用本会话最近
    # 那一组图——有热点文本时绝不混入旧图，否则会拿旧图的线索去跑一条新热点
    group = [item for item in (attachments or []) if item]
    if not group and not text:
        group = list(ctx.recent_attachments or [])
    if str((ctx.operation_binding or {}).get("object_key") or "").startswith("clue:images:") \
            or args.get("clue_version_id"):
        try:
            group = await _bound_image_group(ctx, args, attachments)
        except ApiError as exc:
            return ToolOutcome(status="failed", steps=steps, error=exc.message,
                               payload={"reason": "artifact_version_mismatch"})
    image_cost = 0.0
    image_tokens = (0, 0)
    image_status = ""
    if group:
        await progress("解析图片")
        force_reparse, clue_version_id, binding_error = _image_binding(ctx, args)
        if binding_error:
            return ToolOutcome(
                status="failed",
                steps=steps,
                payload={"reason": binding_error, "force_reparse": force_reparse,
                         "clue_version_id": clue_version_id or None},
                error="这一轮的操作绑定与要求的图片解析不一致，请重新选择后再试",
            )
        try:
            parsed = await _image_clue_for(
                ctx, group, force_reparse=force_reparse,
                clue_version_id=clue_version_id,
            )
        except (ApiError, LLMError, EmbeddingError) as exc:
            # R3（OBS-07）：视觉调用可能已发出、用量未知——如实计入未知，不填零
            return ToolOutcome(
                status="failed",
                steps=steps,
                payload=_failed_image_usage(exc),
                prompt_versions=_failed_image_versions(exc),
                error=f"图片没能解析出来：{_reason(exc)}",
            )
        versions.update(parsed.prompt_versions)
        image_cost = float(parsed.cost_cny or 0.0)
        image_tokens = (int(parsed.prompt_tokens), int(parsed.completion_tokens))
        image_status = str(parsed.usage_status or "")
        clue = parsed.clue.to_dict()
        if not text:
            text = parsed.raw_text.strip()

    if not text:
        return ToolOutcome(
            status="failed",
            steps=steps,
            error="既没有热点文字也没有可用的图片，请先问用户想蹭哪条热点",
        )

    await progress("提交分析")
    clues: list[dict[str, Any] | None] = [clue]
    # A05：分析投递把适用约束快照持久化到本次运行的请求数据，worker 撰稿读同一份
    constraints = await _constraints_for(ctx)
    try:
        submission = await submit_analysis(
            ctx.session, [text], topk=topk, clues=clues,
            context_constraints=constraints, enqueue=ctx.enqueue,
            observation_context={"session_id": str(ctx.session_id or ""),
                                 "turn_id": ctx.turn_id, "tool_call_id": ctx.checkpoint_scope,
                                 "policy": str(ctx.policy.mode if ctx.policy else "off")},
        )
    except ValueError as exc:  # 参数兜底（理论上前面已挡）
        return ToolOutcome(status="failed", steps=steps, error=public_error_message(exc))

    # 提交成功即取得稳定运行引用；等待 worker 的可取消阶段不应阻止它落入轮次账本。
    if group:
        await _consume_image(ctx, parsed)
    _checkpoint(
        ctx,
        "submission",
        run_id=str(submission.run_id),
        job_id=str(submission.job_id),
        status="submitted",
    )

    await progress("分析中", 0, 100)
    status = await _wait_for_run(ctx, submission.job_id, progress)
    if status == "timeout":
        return ToolOutcome(
            status="failed",
            steps=steps,
            # R5（E7OBS2-01）：图片解析**已经消费**的部分照记；分析那段未知单列，不丢已知
            payload={
                "run_id": submission.run_id,
                "usage_status": "unknown",
                "cost_cny": image_cost,
                "prompt_tokens": image_tokens[0],
                "completion_tokens": image_tokens[1],
            },
            error="分析还在跑（超过等待上限），结果稍后可以在运行历史里看到",
            prompt_versions=versions,
        )
    if status == "failed":
        job = await load_job(ctx.session, submission.job_id)
        detail = ((job or {}).get("error") or {}).get("message") or "分析失败"
        return ToolOutcome(
            status="failed",
            steps=steps,
            payload={
                "run_id": submission.run_id,
                "usage_status": "unknown",
                "cost_cny": image_cost,
                "prompt_tokens": image_tokens[0],
                "completion_tokens": image_tokens[1],
            },
            error=str(detail),
            prompt_versions=versions,
        )

    await progress("完成", 100, 100)
    summary = await _summarise(ctx, submission.run_id, text)
    # 这一轮的用量 = 分析那次 run 的用量 + 图片解析的用量（S7.10：图片解析不再"不计成本"）
    summary["cost_cny"] = round(float(summary.get("cost_cny") or 0) + image_cost, 4)
    summary["prompt_tokens"] = int(summary.get("prompt_tokens") or 0) + image_tokens[0]
    summary["completion_tokens"] = (
        int(summary.get("completion_tokens") or 0) + image_tokens[1]
    )
    summary["image_count"] = len(group)
    # R5（E7OBS2-04）：分析 run 的记账来自 worker 记录（供应商 usage 优先、字符估算兜底），
    # 对话层无法逐条核验供应商标记——保守标 estimated（既不像素未知、也不冒充 actual）；
    # 图片段自带口径（actual / estimated / cached），单独可查
    summary["usage_status"] = "estimated"
    if group:
        summary["image_usage_status"] = str(image_status or "estimated")
    return ToolOutcome(
        status="succeeded", steps=steps, payload=summary, prompt_versions=versions
    )


def _failed_image_usage(exc: Exception) -> dict[str, Any]:
    detail = exc.detail if isinstance(exc, ApiError) else {}
    usage = detail.get("usage")
    return dict(usage) if isinstance(usage, dict) else {"usage_status": "unknown"}


def _failed_image_versions(exc: Exception) -> dict[str, int]:
    detail = exc.detail if isinstance(exc, ApiError) else {}
    return {str(key): int(value) for key, value in (detail.get("prompt_versions") or {}).items()}


async def parse_clue_from_image(
    ctx: ToolContext,
    args: dict[str, Any],
    *,
    attachments: list[dict[str, Any]] | None = None,
    on_progress: ProgressFn | None = None,
) -> ToolOutcome:
    """「解析图片」工具（S7.7）：只把图片拆成热点描述与要素，不检索、不落库。"""
    steps: list[dict[str, Any]] = []
    try:
        group = await _bound_image_group(ctx, args, attachments)
    except ApiError as exc:
        return ToolOutcome(status="failed", steps=steps, error=exc.message,
                           payload={"reason": "artifact_version_mismatch"})
    if not group:
        return ToolOutcome(
            status="failed",
            steps=steps,
            error="这一轮没有图片附件（本会话也没有早前的图片），请让用户先发图",
        )
    _step(steps, "解析图片")
    if on_progress is not None:
        await on_progress("解析图片", 0, 0)
    force_reparse, clue_version_id, binding_error = _image_binding(ctx, args)
    if binding_error:
        return ToolOutcome(
            status="failed",
            steps=steps,
            payload={"reason": binding_error, "force_reparse": force_reparse,
                     "clue_version_id": clue_version_id or None},
            error="这一轮的操作绑定与要求的图片解析不一致，请重新选择后再试",
        )
    try:
        parsed = await _image_clue_for(
            ctx, group, force_reparse=force_reparse, clue_version_id=clue_version_id
        )
    except (ApiError, LLMError, EmbeddingError) as exc:
        return ToolOutcome(
            status="failed",
            steps=steps,
            payload=_failed_image_usage(exc),
            prompt_versions=_failed_image_versions(exc),
            error=f"图片没能解析出来：{_reason(exc)}",
        )
    payload = {
        "raw_text": parsed.raw_text,
        "elements": _elements_of(parsed.clue),
        "image_count": len(group),
        "cost_cny": float(parsed.cost_cny or 0.0),
        "prompt_tokens": int(parsed.prompt_tokens),
        "completion_tokens": int(parsed.completion_tokens),
        # R5（E7OBS2-04）：视觉调用的来源口径随结果上卷（actual / estimated / cached）
        "usage_status": str(parsed.usage_status or "estimated"),
        # A08：明确告知这次用的是哪一版（新解析 / 选定编辑版 / 缓存），前端据此展示与切换
        "version_id": parsed.version_id or None,
        "version_no": parsed.version_no or None,
        "cache_status": parsed.cache_status or None,
        "cache_level": parsed.cache_level or None,
        "force_reparse": force_reparse,
    }
    await _consume_image(ctx, parsed)
    return ToolOutcome(
        status="succeeded",
        steps=steps,
        payload=payload,
        prompt_versions=dict(parsed.prompt_versions),
    )


async def search_materials(
    ctx: ToolContext,
    args: dict[str, Any],
    *,
    attachments: list[dict[str, Any]] | None = None,
    on_progress: ProgressFn | None = None,
) -> ToolOutcome:
    """「查素材库」工具（S7.7）：拆解 → 双通道检索 → 候选摘要 + 覆盖率；只读、不落库。"""
    steps: list[dict[str, Any]] = []

    async def progress(
        stage: str,
        done: int = 0,
        total: int = 0,
        *,
        update: dict[str, Any] | None = None,
    ) -> None:
        _step(steps, stage, done, total)
        if on_progress is not None:
            # 结构化 `update` 原样透传（R3 / OBS-12）；None 时不带 kwarg，兼容三参旧实现
            if update is None:
                await on_progress(stage, done, total)
            else:
                await on_progress(stage, done, total, update=update)

    text, invalid = _check_hotspot(args)
    if invalid:
        return ToolOutcome(status="failed", steps=steps, error=invalid)
    topk, invalid_topk = _check_topk(args.get("topk"), ctx.cfg.match.topk)
    if invalid_topk:
        return ToolOutcome(status="failed", steps=steps, error=invalid_topk)

    await progress("拆解要素")
    try:
        record = await _clue_for(ctx, text)
    except (ApiError, LLMError) as exc:
        return ToolOutcome(
            status="failed",
            steps=steps,
            # R5（E7OBS2-01）：拆解请求可能已发出——用量未知如实单列
            payload={"usage_status": "unknown"},
            error=f"热点拆解失败：{_reason(exc)}",
        )

    await progress("检索素材")
    try:
        outcome = await retrieve_candidates(
            ctx.session, ctx.cfg, record.clue, topk=topk, http=ctx.http
        )
    except (ApiError, EmbeddingError, LLMError) as exc:
        return ToolOutcome(
            status="failed",
            steps=steps,
            # R5（E7OBS2-01）：拆解已经消费的部分照记；检索那段未知单列
            payload={
                "usage_status": "unknown",
                "cost_cny": record.cost_cny,
                "prompt_tokens": record.prompt_tokens,
                "completion_tokens": record.completion_tokens,
            },
            error=f"检索素材失败：{_reason(exc)}",
            prompt_versions={CLUE_TASK: record.version},
        )

    candidates = list(outcome.candidates)
    payload = {
        "hotspot": text[:100],
        "elements": _elements_of(record.clue),
        "coverage_ratio": round(float(outcome.coverage.ratio), 3),
        "count": len(candidates),
        "candidates": [
            _candidate_view(item) for item in candidates[:CANDIDATE_PREVIEW_LIMIT]
        ],
        "cost_cny": record.cost_cny,
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
        # R5（E7OBS2-04）：拆解调用的来源口径（缓存命中为 cached，与真实调用区分）
        "usage_status": str(record.usage_status or "estimated"),
    }
    await progress("完成", 100, 100)
    return ToolOutcome(
        status="succeeded",
        steps=steps,
        payload=payload,
        prompt_versions={CLUE_TASK: record.version},
    )


async def read_run(
    ctx: ToolContext,
    args: dict[str, Any],
    *,
    attachments: list[dict[str, Any]] | None = None,
    on_progress: ProgressFn | None = None,
) -> ToolOutcome:
    """「读历史运行」工具（S7.7）：按 run_id 读回已完成的结果；不调模型、不花钱。

    `run_id` 可省：不传就落到本会话最近一次运行产出的那个（见 `_resolve_run_id`）。
    """
    steps: list[dict[str, Any]] = []
    raw, invalid = await _resolve_run_id(ctx, str(args.get("run_id") or ""))
    if invalid:
        return ToolOutcome(status="failed", steps=steps, error=invalid)
    # A04：直接工具入口同样验证归属——合法 UUID 不能读其他会话的产物
    if not await _run_belongs_to_session(ctx, raw):
        return ToolOutcome(
            status="failed",
            steps=steps,
            payload={"reason": "run_not_in_session", "run_id": raw},
            error="这次运行不属于当前会话，不能读取",
        )
    _step(steps, "读取运行")
    detail = await load_run(ctx.session, uuid.UUID(raw))
    if detail is None:
        return ToolOutcome(
            status="failed",
            steps=steps,
            error=f"找不到这次运行（run_id：{raw}），请确认 id 是否正确",
        )
    return ToolOutcome(status="succeeded", steps=steps, payload=_run_summary(detail))


async def preview_element_coverage(
    ctx: ToolContext,
    args: dict[str, Any],
    *,
    attachments: list[dict[str, Any]] | None = None,
    on_progress: ProgressFn | None = None,
) -> ToolOutcome:
    """「拆解预览」工具（S7.7b）：要素 + 字面通道的覆盖估算与补拍建议；不产出候选素材。

    只花一次文本模型钱（拆解），覆盖估算走 `literal_only_candidates`（不调 embedding）；
    不写稿、不落 `runs`、不产生 `job_id`。
    """
    steps: list[dict[str, Any]] = []

    async def progress(
        stage: str,
        done: int = 0,
        total: int = 0,
        *,
        update: dict[str, Any] | None = None,
    ) -> None:
        _step(steps, stage, done, total)
        if on_progress is not None:
            # 结构化 `update` 原样透传（R3 / OBS-12）；None 时不带 kwarg，兼容三参旧实现
            if update is None:
                await on_progress(stage, done, total)
            else:
                await on_progress(stage, done, total, update=update)

    text, invalid = _check_hotspot(args)
    if invalid:
        return ToolOutcome(status="failed", steps=steps, error=invalid)

    await progress("拆解要素")
    try:
        record = await _clue_for(ctx, text)
    except (ApiError, LLMError) as exc:
        return ToolOutcome(
            status="failed",
            steps=steps,
            # R5（E7OBS2-01）：拆解请求可能已发出——用量未知如实单列
            payload={"usage_status": "unknown"},
            error=f"热点拆解失败：{_reason(exc)}",
        )

    await progress("估算覆盖")
    try:
        outcome = await literal_only_candidates(ctx.session, ctx.cfg, record.clue)
    except (ApiError, EmbeddingError, LLMError) as exc:
        return ToolOutcome(
            status="failed",
            steps=steps,
            # R5（E7OBS2-01）：拆解已经消费的部分照记
            payload={
                "usage_status": "unknown",
                "cost_cny": record.cost_cny,
                "prompt_tokens": record.prompt_tokens,
                "completion_tokens": record.completion_tokens,
            },
            error=f"覆盖估算失败：{_reason(exc)}",
            prompt_versions={CLUE_TASK: record.version},
        )

    coverage = outcome.coverage
    payload = {
        "hotspot": text[:100],
        "elements": _elements_of(record.clue),
        "coverage_ratio": round(float(coverage.ratio), 3),
        "covered": [_element_view(item) for item in coverage.covered],
        "gaps": [_element_view(item) for item in coverage.gaps],
        "gap_advice": gap_advice(coverage),
        "materials_considered": outcome.candidates_considered,
        "note": (
            "这一步只做要素拆解与覆盖估算，**不产出候选素材**；"
            "要看具体候选请调 search_materials，要完整分析与文案请调 run_hotspot_analysis"
        ),
        "cost_cny": record.cost_cny,
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
        # R5（E7OBS2-04）：拆解调用的来源口径（缓存命中为 cached）
        "usage_status": str(record.usage_status or "estimated"),
    }
    await progress("完成", 100, 100)
    return ToolOutcome(
        status="succeeded",
        steps=steps,
        payload=payload,
        prompt_versions={CLUE_TASK: record.version},
    )


async def revise_draft(
    ctx: ToolContext,
    args: dict[str, Any],
    *,
    attachments: list[dict[str, Any]] | None = None,
    on_progress: ProgressFn | None = None,
) -> ToolOutcome:
    """「改写文案」工具（S7.8）：按用户要求改写某次运行已产出的初稿。

    **只回对话、不写库**：`run_hotspots.draft` 保持当时的快照（数据契约 §3.2 的口径），
    改后的文案由 supervisor 讲给用户。越界要求返回 `succeeded + refused=true`（拒绝是一次
    成功的判断，不是工具故障）。
    """
    steps: list[dict[str, Any]] = []

    async def progress(
        stage: str,
        done: int = 0,
        total: int = 0,
        *,
        update: dict[str, Any] | None = None,
    ) -> None:
        _step(steps, stage, done, total)
        if on_progress is not None:
            # 结构化 `update` 原样透传（R3 / OBS-12）；None 时不带 kwarg，兼容三参旧实现
            if update is None:
                await on_progress(stage, done, total)
            else:
                await on_progress(stage, done, total, update=update)

    # ---- A04：先读**来源用户消息里已持久化的可信绑定**，不从模型参数重新生成 ----
    binding = dict(ctx.operation_binding or {})
    bound_revise = str(binding.get("operation") or "") == "revise_draft"
    bound_run = ""
    bound_index = 0
    bound_base = str(binding.get("base_version_id") or "") if bound_revise else ""
    if bound_revise:
        bound_object = str(binding.get("object_key") or "")
        parts = bound_object.split(":")
        if len(parts) != 3 or parts[0] != "draft" or not parts[1] or not parts[2].isdigit():
            return ToolOutcome(
                status="failed",
                steps=steps,
                payload={"reason": "artifact_version_mismatch",
                         "object_key": bound_object or None},
                error="这一轮绑定的对象不是某条热点的文案，无法改稿；请重新选择",
            )
        bound_run, bound_index = parts[1], int(parts[2])

    def _bound_conflict(reason: str, error: str) -> ToolOutcome:
        return ToolOutcome(
            status="failed",
            steps=steps,
            payload={
                "reason": reason,
                "object_key": str(binding.get("object_key") or "") or None,
                "bound_base_version_id": bound_base or None,
                "requested_base_version_id": str(args.get("base_version_id") or "") or None,
            },
            error=error,
        )

    raw_arg_run = str(args.get("run_id") or "")
    if bound_run:
        if raw_arg_run and raw_arg_run != bound_run:
            return _bound_conflict(
                "artifact_version_mismatch",
                "模型给出的运行与用户这一轮选定的对象不一致，已拒绝（不覆盖用户选择）",
            )
        raw_run = bound_run
    else:
        raw_run, invalid_run = await _resolve_run_id(ctx, raw_arg_run)
        if invalid_run:
            return ToolOutcome(status="failed", steps=steps, error=invalid_run)
    request = str(args.get("request") or "").strip()
    if not request:
        return ToolOutcome(
            status="failed",
            steps=steps,
            error="缺少 request：请把用户的修改要求整理后传进来（1–500 字）",
        )
    if len(request) > MAX_REQUEST_CHARS:
        return ToolOutcome(
            status="failed",
            steps=steps,
            error=f"修改要求超过 {MAX_REQUEST_CHARS} 字，请让用户说得再短一点",
        )
    raw_index = args.get("hotspot_index")
    if bound_index:
        if raw_index not in (None, "") and str(raw_index) != str(bound_index):
            return _bound_conflict(
                "artifact_version_mismatch",
                "模型给出的热点序号与用户这一轮选定的对象不一致，已拒绝",
            )
        index = bound_index
    elif raw_index is None or raw_index == "":
        index = 1  # 不传就是第一条；0 是非法值，不能顺手当默认
    else:
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return ToolOutcome(
                status="failed",
                steps=steps,
                error="hotspot_index 必须是 ≥1 的整数（第几条热点）",
            )
    if index < 1:
        return ToolOutcome(
            status="failed",
            steps=steps,
            error="hotspot_index 必须是 ≥1 的整数（第几条热点）",
        )

    detail = await load_run(ctx.session, uuid.UUID(raw_run))
    if detail is None:
        return ToolOutcome(
            status="failed",
            steps=steps,
            error=f"找不到这次运行（run_id：{raw_run}），请确认 id 是否正确",
        )
    # A04：直接工具入口也验证归属——UUID 合法不等于属于本会话，不能读别的会话产物。
    if not await _run_belongs_to_session(ctx, raw_run):
        return ToolOutcome(
            status="failed",
            steps=steps,
            payload={"reason": "run_not_in_session", "run_id": raw_run},
            error="这次运行不属于当前会话，不能改写它的文案",
        )
    if str(detail.get("status")) != "succeeded":
        return ToolOutcome(
            status="failed",
            steps=steps,
            error="这次运行还没跑完（或失败了），暂时改不了；稍后可以在运行历史里回看",
        )
    hotspots = list(detail.get("hotspots") or [])
    if index > len(hotspots):
        return ToolOutcome(
            status="failed",
            steps=steps,
            error=f"这次运行只有 {len(hotspots)} 条热点，没有第 {index} 条",
        )
    item = hotspots[index - 1]
    draft = item.get("draft")
    if not isinstance(draft, dict) or not draft:
        return ToolOutcome(
            status="failed",
            steps=steps,
            error="这次运行的这条热点没有文案初稿，没有可改的东西",
        )
    if ctx.caller is None:  # pragma: no cover - 运行器总会注入
        return ToolOutcome(
            status="failed", steps=steps, error="改写失败：对话工具缺少文本模型 caller"
        )
    # ---- S7.25 / v2：对象键 → 初始版本 → 实际基础版本 → 请求认领（先提交再付费）----
    # 版本登记只在 **enforce**（E7CTX-15：shadow 不登记业务版本，off 保持旧行为）；
    # 离线 / 非会话路径（`session=None`）同样不做版本登记。
    policy = ctx.policy
    versioning = (
        ctx.session is not None
        and ctx.session_id is not None
        and (policy is None or policy.commits_versions)
    )
    key = artifact_versions.object_key("draft", raw_run, index)
    base: Any = None
    claim: Any = None
    expected_state_version: int | None = None
    base_draft: dict[str, Any] = dict(draft)
    req_key = ""
    session_obj = ctx.session
    session_uuid = ctx.session_id
    initial_base = await _canonical_base_material(ctx.session, dict(draft))
    initial_evidence, _initial_missing = await _bound_historical_evidence(
        ctx.session,
        item=item, base_draft=initial_base, run_id=raw_run, position=index)
    evidence: dict[str, Any] | None = None
    if versioning and session_obj is not None and session_uuid is not None:
        try:
            initial = await artifact_versions.ensure_initial(
                session_obj,
                session_id=session_uuid,
                object_key_value=key,
                object_type="draft",
                run_id=raw_run,
                position=index,
                content=dict(draft),
                source={"actor": "run_initial"},
                prompt_versions={
                    str(k): int(v)
                    for k, v in (detail.get("prompt_versions") or {}).items()
                },
                evidence_snapshot=initial_evidence,
            )
        except ApiError as exc:
            return ToolOutcome(status="failed", steps=steps, error=_reason(exc))

        base = initial
        raw_base = str(args.get("base_version_id") or "").strip()
        if bound_base:
            # A04：用户这一轮明确选定的基础版优先；模型显式给不同版本 → 拒绝，不覆盖选择
            if raw_base and raw_base != bound_base:
                return _bound_conflict(
                    "artifact_version_mismatch",
                    "模型给出的基础版本与用户这一轮选定的版本不一致，已拒绝",
                )
            raw_base = bound_base
        if raw_base:
            try:
                requested = await artifact_versions.get_version(
                    session_obj, session_uuid, uuid.UUID(raw_base)
                )
            except ValueError:
                requested = None
            if (
                requested is None
                or str(requested.object_key) != key
                or str(requested.status) != "succeeded"
            ):
                return ToolOutcome(
                    status="failed",
                    steps=steps,
                    payload={"object_key": key, "base_version_id": raw_base,
                             "reason": "artifact_version_mismatch"},
                    error="指定的基础版本不属于这条热点的文案（或不是成功版本），请重新选择",
                )
            base = requested
        elif bound_revise:
            # 绑定只给了对象、没指定版本：用该对象最近成功版（服务端已验证的当前选择）
            head = await artifact_versions.head(session_obj, session_uuid, key)
            if head is not None:
                base = head
        else:
            head = await artifact_versions.head(session_obj, session_uuid, key)
            if head is not None:
                base = head

        stored = await context_state.load(session_obj, session_uuid)
        expected_state_version = stored.state_version if stored is not None else None
        # E7CTX-06：基础正文**只**来自版本内容（兼容 draft/revised 两种旧包裹）；
        # 读取失败明确报错，绝不回落到运行初稿——否则"三次改稿"会退回第一次的正文。
        try:
            base_draft = artifact_versions.normalize_draft_content(base.content)
        except ConflictError as exc:
            return ToolOutcome(
                status="failed",
                steps=steps,
                payload={
                    "object_key": key,
                    "base_version_id": str(base.id),
                    "reason": "artifact_version_mismatch",
                },
                error=f"基础版本内容无法读取：{exc.message}",
            )

        base_draft = await _canonical_base_material(ctx.session, base_draft)
        evidence, evidence_missing = await _bound_historical_evidence(
            ctx.session,
            item=item, base_draft=dict(base_draft), run_id=raw_run,
            position=index, base_version=base,
        )
        if evidence is None:
            if claim is not None:  # pragma: no cover - 当前分支尚未认领
                await artifact_versions.finish(
                    ctx.session, claim.row, status="refused",
                    error="historical_evidence_incomplete")
            return ToolOutcome(
                status="failed", steps=steps,
                payload={"reason": "historical_evidence_incomplete",
                         "object_key": key,
                         "base_version_id": str(base.id) if base is not None else None,
                         "missing_fields": evidence_missing},
                error="历史依据不完整，无法在缺少证据的情况下改稿；请回到原运行查看并重新选择",
            )

        req_key = artifact_versions.request_key(
            session_uuid,
            key,
            turn_id=ctx.turn_id,
            call_id=ctx.checkpoint_scope,
            request=request,
        )
        current_head = await artifact_versions.head(session_obj, session_uuid, key)
        claim = await artifact_versions.claim(
            session_obj,
            session_id=session_uuid,
            object_key_value=key,
            object_type="draft",
            run_id=raw_run,
            position=index,
            request_key_value=req_key,
            source={
                "turn_id": ctx.turn_id,
                "tool_call_id": ctx.checkpoint_scope,
                "actor": "revise_draft",
            },
            base_version_id=base.id,
            # E7CTX-07：固化生成前 head / 预期状态版本 / 来源消息
            expected_head_version_id=current_head.id if current_head is not None else None,
            expected_state_version=expected_state_version,
            source_message_id=ctx.source_message_id or None,
            evidence_snapshot=evidence,
        )
        if not claim.is_new and claim.row is not None:
            if claim.status == "succeeded" and isinstance(claim.row.content, dict):
                # 同一请求重放：返回既有结果与**认领时保存的原基础引用**，不再发模型调用
                replay_base = None
                if claim.row.base_version_id:
                    replay_base = await artifact_versions.get_version(
                        session_obj, session_uuid, claim.row.base_version_id
                    )
                await progress("完成", 100, 100)
                return ToolOutcome(
                    status="succeeded",
                    steps=steps,
                    payload={
                        "run_id": raw_run,
                        "hotspot_index": index,
                        "object_key": key,
                        "refused": False,
                        "revised": artifact_versions.normalize_draft_content(claim.row.content),
                        "replayed": True,
                        "base_version_id": str(replay_base.id) if replay_base else None,
                        "base_version_no": int(replay_base.version_no or 0)
                        if replay_base else None,
                        "version_id": str(claim.row.id),
                        "version_no": int(claim.row.version_no or 0),
                        "cost_cny": 0.0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "usage_status": "cached",
                },
                    prompt_versions={
                        str(k): int(v)
                        for k, v in (claim.row.prompt_versions or {}).items()
                },
                )
            if claim.status == "pending":
                return ToolOutcome(
                    status="failed",
                    steps=steps,
                    payload={"object_key": key, "request_key": req_key},
                    error="这次改稿还在处理中（可能上次进程中断了），请稍后重试或先看当前版本",
                )
            # 失败 / 拒绝 / 中断：用新的尝试身份再认领一次，并关联原请求
            req_key = artifact_versions.request_key(
                session_uuid,
                key,
                turn_id=ctx.turn_id,
                call_id=f"{ctx.checkpoint_scope}:retry:{uuid.uuid4().hex[:8]}",
                request=request,
            )
            claim = await artifact_versions.claim(
                session_obj,
                session_id=session_uuid,
                object_key_value=key,
                object_type="draft",
                run_id=raw_run,
                position=index,
                request_key_value=req_key,
                source={
                    "turn_id": ctx.turn_id,
                    "tool_call_id": ctx.checkpoint_scope,
                    "actor": "revise_draft_retry",
                    "retry_of": str(claim.version_id or ""),
                },
                base_version_id=base.id,
                expected_head_version_id=current_head.id
                if current_head is not None else None,
                expected_state_version=expected_state_version,
                source_message_id=ctx.source_message_id or None,
                evidence_snapshot=evidence,
            )
        if claim.row is None:
            return ToolOutcome(
                status="failed", steps=steps, error="改写失败：请求认领没有落库，请稍后重试"
            )

    if evidence is None:
        base_draft = await _canonical_base_material(ctx.session, base_draft)
        evidence, evidence_missing = await _bound_historical_evidence(
            ctx.session,
            item=item, base_draft=dict(base_draft), run_id=raw_run,
            position=index, base_version=None)
        if evidence is None:
            return ToolOutcome(
                status="failed", steps=steps,
                payload={"reason": "historical_evidence_incomplete",
                         "object_key": key, "missing_fields": evidence_missing},
                error="历史依据不完整，无法在缺少证据的情况下改稿；请回到原运行查看并重新选择",
            )
    # E8：只用冻结证据里的素材文字；当前素材是否存在不改变改稿依据。
    assert evidence is not None
    material = dict(evidence["material"])
    base_draft = {**base_draft, "material_id": str(evidence["canonical_material_id"])}
    base_draft.pop("_original_material_id", None)
    # A05：适用约束快照进入真正的改稿请求（不是只在 supervisor 里提示）
    constraints = await _constraints_for(ctx, object_key=key)
    await progress("改写文案")
    before = len(ctx.caller.records)
    try:
        outcome = await revise_agent(
            clue=HotspotClue.from_dict(evidence.get("clue") or {}),
            material=material,
            draft=dict(base_draft),
            request=request,
            caller=ctx.caller,
            constraints=constraints,
        )
    except (ApiError, LLMError, SchemaError) as exc:
        if claim is not None:
            await artifact_versions.finish(
                ctx.session, claim.row, status="failed", error=_reason(exc)
            )
        return ToolOutcome(
            status="failed",
            steps=steps,
            payload={
                "usage_status": "unknown",
                "object_key": key,
                "base_version_id": str(base.id) if base is not None else None,
                "base_version_no": int(base.version_no or 0) if base is not None else None,
            },
            error=f"改写失败：{_reason(exc)}",
        )

    cost = _records_cost(ctx.caller, before)
    prompt_tokens, completion_tokens = _records_tokens(ctx.caller, before)
    usage_status = _records_usage_status(ctx.caller, before) or "estimated"
    usage = {
        "cost_cny": cost,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "usage_status": usage_status,
    }
    result = outcome.value
    assert isinstance(result, ReviseOutcome)  # 由 `revise_agent` 保证
    versions = {REVISE_TASK: int(outcome.version or 1)}
    await progress("完成", 100, 100)
    version_payload = {
        "object_key": key,
        "base_version_id": str(base.id) if base is not None else None,
        "base_version_no": int(base.version_no or 0) if base is not None else None,
    }
    if result.refused:
        if claim is not None:
            await artifact_versions.finish(
                ctx.session, claim.row, status="refused", error=result.reason, usage=usage
            )
        return ToolOutcome(
            status="succeeded",
            steps=steps,
            payload={
                "run_id": raw_run,
                "hotspot_index": index,
                "refused": True,
                "reason": result.reason,
                **version_payload,
                "cost_cny": cost,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "usage_status": usage_status,
            },
            prompt_versions=versions,
        )
    revised = result.draft.to_dict() if result.draft is not None else {}
    evidence_notice = "基于历史文字依据生成"
    if (evidence.get("current_material") or {}).get("exists") is False:
        evidence_notice += "，原素材当前不可用"
    material_origin = ((evidence.get("sources") or {}).get("material") or {}).get("origin")
    clue_origin = ((evidence.get("sources") or {}).get("clue") or {}).get("origin")
    if "cutover_capture" in (material_origin, clue_origin):
        evidence_notice += "；部分依据来自切换时采集"
    committed = None
    if claim is not None:
        try:
            committed = await artifact_versions.commit_success(
                ctx.session,
                claim_row=claim.row,
                # E7CTX-06：新版本 content 统一存完整 Draft（数据契约 §3.9）
                content=dict(revised),
                prompt_versions=versions,
                usage=usage,
                expected_state_version=expected_state_version,
                evidence_snapshot=evidence,
            )
        except ConflictError as exc:
            await artifact_versions.finish(
                ctx.session, claim.row, status="failed", error=exc.message, usage=usage
            )
            return ToolOutcome(
                status="failed",
                steps=steps,
                payload={**version_payload, "conflict": True, "usage_status": usage_status},
                error=f"{exc.message}（已发生的调用费用照记，未覆盖新版本）",
            )
    return ToolOutcome(
        status="succeeded",
        steps=steps,
        payload={
            "run_id": raw_run,
            "hotspot_index": index,
            "refused": False,
            "revised": revised,
            "evidence_notice": evidence_notice,
            "evidence_sources": evidence.get("sources") or {},
            "current_material": evidence.get("current_material") or {},
            "replayed": False,
            **version_payload,
            "version_id": str(committed.id) if committed is not None else None,
            "version_no": int(committed.version_no or 0) if committed is not None else None,
            "cost_cny": cost,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            # R5（E7OBS2-04）：改写调用的来源口径随结果上卷
            "usage_status": usage_status,
        },
        prompt_versions=versions,
    )




async def _read_image(
    ctx: ToolContext, attachment: dict[str, Any]
) -> tuple[bytes, str]:
    """读附件字节并确认它是受支持的图片（magic bytes；与图片解析接口同一口径）。"""
    relative = str(attachment.get("path") or "")
    if relative:
        resolved = resolve_attachment(ctx.cfg, ctx.session_id or "", relative)
        data = await asyncio.to_thread(Path(resolved).read_bytes)
    mime = sniff_image_mime(data)
    if not mime:
        raise BadRequestError(
            "附件不是 jpg / png / webp 图片"
        )  # 交给调用方折成工具失败
    return data, mime


def _stage_update(
    job: dict[str, Any], job_id: str, snapshot: dict[str, Any], stage_key: str
) -> dict[str, Any]:
    """把 `runs.stage` 快照投影成对话侧的结构化阶段更新（R3 / OBS-05）。

    保留**来源身份与真实状态**：`stage_key` / 状态 / 源尝试号 / 投递重试次数 / 安全错误类别 /
    耗时（终态节点上的 `latency_ms`）/ `run_id` / `job_id`；没有阶段键时返回空 dict
    （调用方退回"分析中"心跳）。
    """
    if not stage_key:
        return {}
    key = str(stage_key)
    attempt = int(snapshot.get("attempt") or 1)
    latency_ms = 0
    for node in snapshot.get("nodes") or []:
        if (
            isinstance(node, dict)
            and str(node.get("stage") or "") == key
            and int(node.get("attempt") or 1) == attempt
            and str(node.get("status") or "") == str(snapshot.get("status") or "")
        ):
            latency_ms = int(node.get("latency_ms") or 0)
            break
    result = {
        "stage_key": key,
        "label": progress_tool.label_for(key),
        "status": str(snapshot.get("status") or "running"),
        "attempt": attempt,
        "retries": int(snapshot.get("retries") or 0),
        "error_category": str(snapshot.get("error_category") or ""),
        "latency_ms": latency_ms,
        "run_id": str(job.get("run_id") or ""),
        "job_id": str(job.get("job_id") or job_id),
    }
    # 旧快照保持原形；新来源字段只有在生产端确实提供时才跨边界暴露。
    for key in ("source_id", "stage_instance_id", "source_seq", "progress_revision",
                "generation"):
        value = snapshot.get(key)
        if value not in (None, "", 0):
            result[key] = value
    return result


def _update_marker(update: dict[str, Any]) -> tuple[Any, ...]:
    """结构化更新的去重键：内容没变就不重复上报（不重复追加节点 / 不逐轮询写库）。

    R5（E7OBS2-03）：键里带**来源身份**（`run_id` / `job_id`）——不同来源的同名阶段不是
    同一条事实，不能静默去重；终态带耗时（`latency_ms`），后补的耗时修订才能到达对话侧。
    运行中不带耗时（避免把逐轮询的进度采样当变化）。
    """
    return (
        update.get("stage_key"),
        update.get("status"),
        int(update.get("attempt") or 1),
        int(update.get("retries") or 0),
        str(update.get("error_category") or ""),
        str(update.get("run_id") or ""),
        str(update.get("job_id") or ""),
        int(update.get("latency_ms") or 0) if update.get("status") != "running" else 0,
        # E7OBS4-03：来源 / 实例 / 序号 / 代次属于阶段事实，不能静默去重
        str(update.get("source_id") or ""),
        str(update.get("stage_instance_id") or ""),
        int(update.get("source_seq") or 0),
        int(update.get("generation") or 0),
    )


async def _wait_for_run(
    ctx: ToolContext, job_id: str, progress: Callable[..., Awaitable[None]]
) -> str:
    """轮询 `runs` 到终态；返回 `succeeded` / `failed` / `timeout`。

    S7.20 / R3：除中文标签进度外，把 `runs.stage` 快照**结构化转发**（阶段 / 真实状态 / 源尝试号 /
    重试次数 / 错误类别 / run_id / job_id）——对话侧据此合并出忠实区分 running / failed /
    skipped / 重试的快照；终态 job 的最后一份快照在返回前补发一次，结构化内容没变化时不重复上报。
    快速阶段被轮询略过时**不补造**记录（观测缺失就如实缺席）。
    """
    limit = (
        ctx.max_wait_s
        if ctx.max_wait_s is not None
        else float(ctx.cfg.queue.task_time_limit_s)
    )
    deadline = time.monotonic() + limit
    last_marker: tuple[Any, ...] = ()
    last_source: tuple[str, str] = ("", "")
    while True:
        # 同一个 session 里必须显式过期：否则 ORM 会把内存里的旧对象原样返回（poll 永远不变）。
        # `expire_all()` 是同步方法（只清身份映射，不发 SQL）。
        ctx.session.expire_all()
        job = await load_job(ctx.session, job_id)
        job = job or {}
        # R5（E7OBS2-03）：单个 job_id 的来源（run / job）本应固定；变化不是静默去重，
        # 而是**如实报告**（乱序 / 串线迹象）并继续按新来源上报
        source = (str(job.get("run_id") or ""), str(job.get("job_id") or job_id))
        if last_source != ("", "") and source != last_source:
            logger.warning(
                "分析 job 来源发生变化：run=%s job=%s → run=%s job=%s（按新来源上报）",
                last_source[0],
                last_source[1],
                source[0],
                source[1],
            )
        last_source = source
        status = str(job.get("status") or "")
        snapshot = job.get("stage") or {}
        stage_key = str(snapshot.get("stage") or "")
        if not stage_key and status == "queued":
            stage_key = "queue_wait"  # 排队等待由状态推导，不落单独阶段
        percent = int(round(float(job.get("progress") or 0) * 100))
        update = _stage_update(job, job_id, snapshot, stage_key)
        if update and _update_marker(update) != last_marker:
            last_marker = _update_marker(update)
            # 阶段切换的那一次用阶段标签代替"分析中"：每条轮询仍只发一个进度事件
            await progress(str(update["label"]), percent, 100, update=update)
        else:
            await progress("分析中", percent, 100)
        if status in RUN_TERMINAL:
            # 终态 job：最后一份快照（含 failed / skipped 节点）在返回前补发一次
            final = _stage_update(job, job_id, snapshot, stage_key)
            if final and _update_marker(final) != last_marker:
                await progress(str(final["label"]), 100, 100, update=final)
            return status
        if time.monotonic() >= deadline:
            return "timeout"
        await ctx.sleep(ctx.poll_interval_s)


async def _summarise(ctx: ToolContext, run_id: str, hotspot: str) -> dict[str, Any]:
    """把 run 详情压成给小模型看的一小块（不把整份 RunDetail 塞进上下文）。"""
    detail = await load_run(ctx.session, uuid.UUID(str(run_id))) or {}
    totals = detail.get("totals") or {}
    hotspots = detail.get("hotspots") or []
    first = hotspots[0] if hotspots else {}
    candidates = first.get("candidates") or []
    top = candidates[0] if candidates else {}
    draft = first.get("draft") or {}
    return {
        "run_id": str(run_id),
        "hotspot": hotspot[:100],
        "status": detail.get("status") or "succeeded",
        # 键名与 `search_materials` / `read_run` 统一：`count` 是总数，`candidates` 是清单
        "count": len(candidates),
        "candidates": _candidate_briefs(candidates),
        "top1_title": (top.get("material") or {}).get("title") or "",
        "top1_score": round(float(top.get("score") or 0), 3),
        "coverage_ratio": round(
            float((first.get("coverage") or {}).get("ratio") or 0), 3
        ),
        "draft_titles": [item.get("text") for item in (draft.get("titles") or [])][:3],
        "has_draft": bool(draft),
        "cost_cny": float(totals.get("cost_cny") or 0),
        "prompt_tokens": int(totals.get("prompt_tokens") or 0),
        "completion_tokens": int(totals.get("completion_tokens") or 0),
        "latency_ms": int(totals.get("latency_ms") or 0),
    }


def _run_summary(detail: dict[str, Any]) -> dict[str, Any]:
    """整份 run 详情的精简形状（`read_run` 用）：每个热点只留能回话的字段。

    候选清单必须整份带出来（P0-1）：用户回头看「第 4 条」时，只有这里能给出与那次运行一致
    的 rank / 标题 / usage。少给这一层，模型就只能重新检索，于是拿到一份新排序，再拿新排序
    去否定用户——这正是「助手把对的说成错的」的成因。
    """
    totals = detail.get("totals") or {}
    hotspots: list[dict[str, Any]] = []
    for index, item in enumerate(detail.get("hotspots") or [], start=1):
        candidates = item.get("candidates") or []
        top = candidates[0] if candidates else {}
        draft = item.get("draft") or {}
        hotspots.append(
            {
                "index": index,
                "hotspot": str(item.get("hotspot_raw") or "")[:100],
                "elements": _elements_of(item.get("clue") or {}),
                "coverage_ratio": round(
                    float((item.get("coverage") or {}).get("ratio") or 0), 3
                ),
                "count": len(candidates),
                "candidates": _candidate_briefs(candidates),
                "top1_title": (top.get("material") or {}).get("title") or "",
                "draft_titles": [
                    entry.get("text") for entry in (draft.get("titles") or [])
                ][:3],
            }
        )
    return {
        "run_id": detail.get("run_id"),
        "status": detail.get("status"),
        "created_at": detail.get("created_at"),
        "totals": {
            "llm_calls": totals.get("llm_calls"),
            "cost_cny": float(totals.get("cost_cny") or 0),
            "prompt_tokens": int(totals.get("prompt_tokens") or 0),
            "completion_tokens": int(totals.get("completion_tokens") or 0),
            "latency_ms": int(totals.get("latency_ms") or 0),
        },
        "prompt_versions": detail.get("prompt_versions") or {},
        "hotspots": hotspots,
    }


# ---------- 网页两件（PR2） ----------
# 适配器在 `tools/`（`websearch.search_web` / `webreader.fetch_readable`），本段只做「配额 →
# 缓存 → 追踪 → 错误映射」。两条铁律：① 任何失败都只是 `failed` + 中文原因，绝不抛出
# （抛出会触发节点重试，重复付费）；② 只有真正发出请求才 `web_calls += 1`，缓存命中不计数。


def _quota_left(ctx: ToolContext) -> bool:
    """这一轮还能不能再发网页请求（与 `[chat].max_tool_rounds` 分别生效）。"""
    return ctx.cache.web_calls < int(ctx.cfg.web.max_calls_per_turn)


async def _web_stage(
    steps: list[dict[str, Any]], on_progress: ProgressFn | None, stage: str
) -> None:
    """工具过程里的一步：既记进 `outcome.steps`，也推给前端（三参旧实现照样能收）。"""
    _step(steps, stage)
    if on_progress is not None:
        await on_progress(stage)


def _engine_failures(unresponsive: list[list[str]]) -> list[tuple[str, str]]:
    """`unresponsive_engines` →（引擎名，原因）；结构异常的元素丢掉，不为它整次失败。"""
    failures: list[tuple[str, str]] = []
    for entry in unresponsive or []:
        parts = [str(part) for part in entry] if isinstance(entry, list) else []
        if not parts or not parts[0]:
            continue
        failures.append((parts[0], parts[1] if len(parts) > 1 else "无响应"))
    return failures


def _search_note(count: int, unresponsive: list[list[str]]) -> str:
    """搜索结果的「人话补充」：只在有引擎没响应或 0 条命中时非空，其余留空串。"""
    pieces: list[str] = []
    failures = _engine_failures(unresponsive)
    if failures:
        shown = "、".join(f"{name}（{reason}）" for name, reason in failures[:5])
        pieces.append(f"{len(failures)} 个搜索引擎没有返回结果：{shown}，结果可能不完整")
    if not count:
        pieces.append("没搜到结果：换个说法或换个方向再试，不要拿这批结果编造热点")
    return "；".join(pieces)


# 适配器错误 reason → 能直接回给用户、也能指导模型下一步的一句中文（不做静默降级）
_SEARCH_ERROR_TEXT = {
    "config": "搜索实例没配好（地址或 JSON 输出没开），请联系管理员检查 [web] 配置",
    "timeout": "搜索超时了，先按现有结果作答，稍后再试",
    "http": "搜索实例没能正常响应（网络或服务异常），先按现有结果作答",
    "schema": "搜索实例返回的结构不对（多半是没开 JSON 输出），先按现有结果作答",
}

_FETCH_ERROR_TEXT = {
    "scheme": "只支持 http(s) 开头的完整链接，请从搜索结果或用户消息里原样取",
    "blocked_host": "这个地址指向内网，出于安全考虑不抓取",
    "dns": "域名解析不出来，可能链接写错了",
    "robots": "目标站点在 robots.txt 里禁止抓取，换一个来源再试",
    "timeout": "读取超时了，先按现有结果作答，稍后再试",
    "http": "站点报错或连不上，换一个来源再试",
    "too_large": "页面太大（超出单次上限），换一个来源或让用户说明要点",
    "content_type": "这个链接不是网页正文（可能是 PDF / 图片），读不了",
    "insufficient_text": "页面没抽到正文（可能需要 JS 渲染），换一个来源再试",
}


def _search_error(exc: WebSearchError) -> str:
    """`WebSearchError` → 一句中文原因（reason 白名单外的值也给出可执行的退路）。"""
    return "搜索失败：" + _SEARCH_ERROR_TEXT.get(
        exc.reason, "搜索实例出错，先按现有结果作答"
    )


def _fetch_error(exc: WebFetchError) -> str:
    """`WebFetchError` → 一句中文原因（reason 白名单外的值也给出可执行的退路）。"""
    return "读取网页失败：" + _FETCH_ERROR_TEXT.get(
        exc.reason, "页面没能处理，先按现有结果作答"
    )


async def search_hotspots(
    ctx: ToolContext,
    args: dict[str, Any],
    *,
    attachments: list[dict[str, Any]] | None = None,
    on_progress: ProgressFn | None = None,
) -> ToolOutcome:
    """「搜热点」工具（PR2）：一次搜索 → 候选标题 / 链接 / 摘要；只读、不花钱。

    顺序是**校验 → 配额 → 缓存 → 请求**：配额用完连缓存也不再给（模型据此收手，用现有
    结果作答）。缓存键是 `query`（Task 5 定），命中只回放、不增加 `web_calls`。
    """
    steps: list[dict[str, Any]] = []
    query, invalid = _check_query(args)
    if invalid:
        return ToolOutcome(status="failed", steps=steps, error=invalid)
    count, invalid_count = _check_count(args.get("count"))
    if invalid_count:
        return ToolOutcome(status="failed", steps=steps, error=invalid_count)
    if not _quota_left(ctx):
        return ToolOutcome(status="failed", steps=steps, error=TURN_LIMIT_MESSAGE)

    cached = ctx.cache.searches.get(query)
    if cached is not None:
        results = [dict(item) for item in cached[:count]]
        _step(steps, "命中缓存")
        return ToolOutcome(
            status="succeeded",
            steps=steps,
            payload={
                "query": query,
                "count": len(results),
                "results": results,
                "note": _search_note(len(results), []),
                "cached": True,
            },
        )

    ctx.cache.web_calls += 1
    await _web_stage(steps, on_progress, "搜索热点")
    active = get_tracer(ctx.cfg)
    try:
        with active.node(
            name="tool.web_search", metadata={"stage": "web_search"}
        ) as fields:
            try:
                found = await search_web(None, ctx.cfg, query, count=count)
            except BaseException as exc:   # 先把安全类别写进 span，再交给下面的映射
                fields.level = LEVEL_ERROR
                fields.status_message = progress_tool.error_category(exc)
                raise
    except WebSearchError as exc:
        return ToolOutcome(status="failed", steps=steps, error=_search_error(exc))
    except Exception as exc:  # noqa: BLE001 - 工具层不抛异常（抛出会触发节点重试）
        logger.warning("搜索工具失败：%s", type(exc).__name__)
        return ToolOutcome(
            status="failed",
            steps=steps,
            error="搜索失败：搜索实例返回了无法处理的结果，先按现有结果作答",
        )

    results = [
        {
            "title": item.title,
            "url": item.url,
            "snippet": item.snippet,
            "published_date": item.published_date,
        }
        for item in found.hits
    ]
    ctx.cache.searches[query] = [dict(item) for item in results]
    # 本任务的关键接线：搜到的 url 记进 `seen_urls`，之后抓它就是「自主调用」
    ctx.cache.seen_urls.update(str(item["url"]) for item in results)
    return ToolOutcome(
        status="succeeded",
        steps=steps,
        payload={
            "query": query,
            "count": len(results),
            "results": [dict(item) for item in results],
            "note": _search_note(len(results), found.unresponsive),
        },
    )


async def fetch_url(
    ctx: ToolContext,
    args: dict[str, Any],
    *,
    attachments: list[dict[str, Any]] | None = None,
    on_progress: ProgressFn | None = None,
) -> ToolOutcome:
    """「读网页」工具（PR2）：打开一个公网页 → 正文 + 分页游标；只读、不落盘。

    `autonomous` 由 **`url in ctx.cache.seen_urls`** 决定：本轮搜到的才走自主 UA 与
    robots.txt，用户直接贴的链接按人工调用处理。缓存键是 `(url, start_index)`：只用 url
    会让第二页永远读到第一页，续读（`start_index > 0`）算一次新的 `web_calls`。
    """
    steps: list[dict[str, Any]] = []
    url, invalid = _check_url(args)
    if invalid:
        return ToolOutcome(status="failed", steps=steps, error=invalid)
    start_index, invalid_start = _check_start_index(args.get("start_index"))
    if invalid_start:
        return ToolOutcome(status="failed", steps=steps, error=invalid_start)
    if not _quota_left(ctx):
        return ToolOutcome(status="failed", steps=steps, error=TURN_LIMIT_MESSAGE)

    cached = ctx.cache.pages.get((url, start_index))
    if cached is not None:
        _step(steps, "命中缓存")
        return ToolOutcome(
            status="succeeded", steps=steps, payload={**cached, "cached": True}
        )

    ctx.cache.web_calls += 1
    await _web_stage(steps, on_progress, "读取网页")
    autonomous = url in ctx.cache.seen_urls
    active = get_tracer(ctx.cfg)
    try:
        with active.node(
            name="tool.web_fetch",
            metadata={
                "stage": "web_fetch",
                "autonomous": autonomous,
                "start_index": start_index,
            },
        ) as fields:
            try:
                page = await fetch_readable(
                    None,
                    ctx.cfg,
                    url,
                    autonomous=autonomous,
                    start_index=start_index,
                    robots_cache=ctx.cache.robots,
                )
            except BaseException as exc:   # 先把安全类别写进 span，再交给下面的映射
                fields.level = LEVEL_ERROR
                fields.status_message = progress_tool.error_category(exc)
                raise
    except WebFetchError as exc:
        return ToolOutcome(status="failed", steps=steps, error=_fetch_error(exc))
    except Exception as exc:  # noqa: BLE001 - 工具层不抛异常（抛出会触发节点重试）
        logger.warning("抓取工具失败：%s", type(exc).__name__)
        return ToolOutcome(
            status="failed",
            steps=steps,
            error="读取网页失败：页面没能处理，先按现有结果作答",
        )

    payload = {
        "url": url,
        "final_url": page.final_url,
        "title": page.title,
        "text": page.text,
        "char_count": page.char_count,
        "truncated": page.truncated,
        "next_start_index": page.next_start_index,
    }
    ctx.cache.pages[(url, start_index)] = dict(payload)
    return ToolOutcome(status="succeeded", steps=steps, payload=payload)


def tool_result_note(outcome: ToolOutcome) -> str:
    """给日志/调试用的一句话（不含正文）。"""
    if outcome.status == "succeeded":
        return (
            f"run={os.path.basename(str(outcome.payload.get('run_id') or ''))} "
            f"candidates={outcome.payload.get('candidates', outcome.payload.get('count', 0))}"
        )
    return f"失败：{outcome.error}"


# ---------- 分发表（S7.7a） ----------
# **加工具只改这个文件**：① 上面 `TOOLS` 加一条 function schema；② 这里加一条 name → handler 映射。
# 网页两件（PR2）是例外：schema 在 `TOOLS` 外面的 `TOOL_SCHEMA_*`（由 `tools_for` 按开关追加），
# 但 name → handler 映射照常加在这张表里。
# 对话图（`workflows/chat.py`）按**本轮清单**（`tools_for(cfg)` 的结果）做已知/未知划分、
# 按名字调 handler，自身不出现任何具体工具名——有源码扫描用例
# （`tests/unit/test_chat_graph.py`）钉住这条，扫描范围是默认名 ∪ 网页两名。

HANDLERS: dict[str, ToolHandler] = {
    TOOL_RUN_ANALYSIS: run_hotspot_analysis,
    TOOL_PARSE_IMAGE: parse_clue_from_image,
    TOOL_SEARCH_MATERIALS: search_materials,
    TOOL_READ_RUN: read_run,
    TOOL_PREVIEW_COVERAGE: preview_element_coverage,
    TOOL_REVISE_DRAFT: revise_draft,
    TOOL_SEARCH_HOTSPOTS: search_hotspots,
    TOOL_FETCH_URL: fetch_url,
}


def web_enabled(cfg: AppConfig) -> bool:
    """网页能力是否下发给对话 agent：**只看总开关，不看密钥**。

    私有 SearXNG 无鉴权，密钥为空不影响功能（《配置契约》§3.14）；把密钥当启用前提会让
    无鉴权实例永远开不起来。
    """
    return bool(cfg.web.enabled)


def tools_for(cfg: AppConfig | None = None) -> list[dict[str, Any]]:
    """本轮下发给模型的工具清单：关闭态与 `TOOLS` **逐项相等**，开启才追加网页工具。

    `cfg=None`（离线评测 / 还没接线的调用方）与关闭态同义。每次现算并**深拷贝**：
    `TOOLS` 的元素与 `TOOL_SCHEMA_*` 都是模块级 dict，浅拷贝等于把全局清单借给调用方
    （就地改嵌套字段会回流，下一轮就发错了）。每轮一次 deepcopy 是微秒级成本。
    """
    tools = copy.deepcopy(TOOLS)
    if cfg is None or not web_enabled(cfg):
        return tools
    tools.append(copy.deepcopy(TOOL_SCHEMA_SEARCH_HOTSPOTS))
    if cfg.web.fetch_enabled:
        tools.append(copy.deepcopy(TOOL_SCHEMA_FETCH_URL))
    return tools


def tool_names(cfg: AppConfig | None = None) -> set[str]:
    """`tools_for(cfg)` 里声明的工具名集合（每次现算：读模块属性而不是导入时快照，
    monkeypatch 换掉 `TOOLS` 也能被发现）。

    图**只在构图时**读一次清单（`run_turn` 把 `tools_for(cfg)` 传给 `_build_graph`）；本函数
    是给图外调用方（测试 / 评测 / 边界用例）用的同一口径。无参 = 全量 = `TOOLS` 的名字
    （S7.7a 起的旧口径）；传 `cfg` 时跟着开关走。
    """
    return {str(item["function"]["name"]) for item in tools_for(cfg)}


def effective_max_rounds(cfg: AppConfig) -> int:
    """一轮对话最多几次工具调用：网页能力开启时**至少** `WEB_MAX_TOOL_ROUNDS`，否则按 `[chat]`。

    取 `max(...)` 而不是写死 8：用户把 `[chat].max_tool_rounds` 调得更大时不许被压回去。
    """
    rounds = int(cfg.chat.max_tool_rounds)
    if web_enabled(cfg):
        return max(rounds, WEB_MAX_TOOL_ROUNDS)
    return rounds
