"""Prompt 资产加载与渲染（prompt 契约 §三 / §四）。

- 资源位置：`src/xhs_agent/prompts/<task_id>.md`，随 wheel 分发，用 importlib.resources 读取。
- 五段：01 System / 02 Task / 03 Constraints / 04 Examples / 05 Output Schema。
- 组装：system = 具名 01 / 03 / 04 / 05；user = 具名 02 Task（渲染后）。
- 占位符 `{{name}}` **只在 02 Task 段生效**，其它段里出现的按字面文本处理。
- 同目录另有**机读能力清单** `chat_capabilities.json`（S7.11）：它不是 prompt 资产、不进
  `available_tasks()`，由 `load_capabilities()` / `render_capabilities()` 读写（ADR 0016）。
- 未知 task_id / 缺段 / 缺变量 / requires 与实际占位符不符，一律抛 PromptError——
  不做静默降级（AGENTS.md 第 4 节，docs/contracts/prompt契约.md §四）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any

PACKAGE = "xhs_agent.prompts"
SUFFIX = ".md"
# 机读能力清单（S7.11）：三类边界，顺序即契约（ADR 0016）
CAPABILITIES_NAME = "chat_capabilities.json"
CAPABILITY_CATEGORIES: tuple[str, ...] = (
    "in_scope",
    "out_of_scope_guidable",
    "compliance_red_line",
)

# 五段的顺序与标题必须逐字一致（契约 §三）
SECTION_TITLES: tuple[str, ...] = (
    "01 System",
    "02 Task",
    "03 Constraints",
    "04 Examples",
    "05 Output Schema",
)
SECTION_KEYS: tuple[str, ...] = (
    "system",
    "task",
    "constraints",
    "examples",
    "output_schema",
)

SECTION_SEPARATOR = "\n\n"

_SECTION_RE = re.compile(r"^##\s+(0[1-5])\s+(.+?)\s*$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_VERSION_RE = re.compile(r"<!--\s*prompt-version:\s*v(\d+)\s*-->")
_TASK_ID_RE = re.compile(r"<!--\s*task-id:\s*([A-Za-z_][A-Za-z0-9_]*)\s*-->")
_REQUIRES_RE = re.compile(r"<!--\s*requires:\s*([^>]*?)\s*-->")


class PromptError(RuntimeError):
    """prompt 资产缺失、格式不合契约，或渲染时变量不全。"""


@dataclass(frozen=True)
class RenderedPrompt:
    """渲染结果：直接可用于 messages 的 system 与 user 文本。"""

    task_id: str
    version: int
    system: str
    user: str


@dataclass(frozen=True)
class Prompt:
    """一份已解析的 prompt 资产。"""

    task_id: str
    version: int
    sections: dict[str, str]
    requires: tuple[str, ...]

    def placeholders(self) -> set[str]:
        """02 Task 段里实际出现的占位符集合。"""
        return set(_PLACEHOLDER_RE.findall(self.sections["task"]))

    def render(self, **values: object) -> RenderedPrompt:
        """按契约 §四 组装 system / user；变量不全即报错。"""
        provided = {name: value for name, value in values.items() if value is not None}
        missing = sorted(self.placeholders() - set(provided))
        if missing:
            raise PromptError(
                f"[{self.task_id}] 缺少变量：{', '.join(missing)}；"
                f"本 prompt 声明的变量为 {', '.join(self.requires) or '（无）'}")
        task_body = _PLACEHOLDER_RE.sub(lambda m: str(provided[m.group(1)]),
                                        self.sections["task"])
        system = SECTION_SEPARATOR.join(
            f"## {SECTION_TITLES[index]}\n\n{self.sections[SECTION_KEYS[index]]}"
            for index in (0, 2, 3, 4))
        user = f"## {SECTION_TITLES[1]}\n\n{task_body}"
        return RenderedPrompt(task_id=self.task_id, version=self.version,
                              system=system, user=user)


def parse(text: str, task_id: str | None = None) -> Prompt:
    """解析 prompt 文本；格式不合契约时抛 PromptError。"""
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        raise PromptError("未找到段标题；文件必须含 `## 01 System` 至 `## 05 Output Schema`")
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        number = int(match.group(1))
        actual_title = f"{match.group(1)} {match.group(2)}"
        expected_title = SECTION_TITLES[number - 1]
        if actual_title != expected_title:
            raise PromptError(f"段标题必须逐字为「{expected_title}」，实际是「{actual_title}」")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[SECTION_KEYS[number - 1]] = text[match.end():end].strip()

    missing = [SECTION_TITLES[SECTION_KEYS.index(key)]
               for key in SECTION_KEYS if key not in sections]
    if missing:
        raise PromptError(f"缺少段：{', '.join(missing)}")

    header = text[:matches[0].start()]
    version_match = _VERSION_RE.search(header)
    if version_match is None:
        raise PromptError("头部缺少版本号，应形如 `<!-- prompt-version: v1 -->`")
    version = int(version_match.group(1))
    if version < 1:
        raise PromptError(f"版本号必须从 v1 起，实际为 v{version}")

    meta_task = _TASK_ID_RE.search(header)
    meta_task_id = meta_task.group(1) if meta_task else ""
    if task_id and meta_task_id and task_id != meta_task_id:
        raise PromptError(f"请求的任务名 {task_id} 与文件头声明的 {meta_task_id} 不一致")
    resolved_task_id = task_id or meta_task_id
    if not resolved_task_id:
        raise PromptError("无法确定 task_id：请传入 task_id 或在文件头声明 `<!-- task-id: ... -->`")

    requires_match = _REQUIRES_RE.search(header)
    requires = tuple(
        item.strip() for item in (requires_match.group(1).split(",") if requires_match else [])
        if item.strip()
    )

    prompt = Prompt(task_id=resolved_task_id, version=version, sections=sections,
                    requires=requires)
    actual = prompt.placeholders()
    if set(requires) != actual:
        only_declared = sorted(set(requires) - actual)
        only_used = sorted(actual - set(requires))
        raise PromptError(
            f"[{resolved_task_id}] requires 与 02 Task 的实际占位符不一致："
            f"只在 requires {only_declared}／只在正文 {only_used}")
    return prompt


def load(task_id: str) -> Prompt:
    """按 task_id 读取并解析 prompt 文件。"""
    path = resources.files(PACKAGE).joinpath(f"{task_id}{SUFFIX}")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptError(
            f"未找到 prompt 文件：{task_id}{SUFFIX}；可用任务："
            f"{', '.join(available_tasks()) or '（无）'}") from exc
    return parse(text, task_id=task_id)


def render(task_id: str, **values: object) -> RenderedPrompt:
    """读取并渲染；等价于 `load(task_id).render(**values)`。"""
    return load(task_id).render(**values)


def available_tasks() -> tuple[str, ...]:
    """当前包内可用的 task_id 列表。"""
    return tuple(sorted(
        entry.name[: -len(SUFFIX)]
        for entry in resources.files(PACKAGE).iterdir()
        if entry.name.endswith(SUFFIX)
    ))


def protected_prompt_texts() -> tuple[str, ...]:
    """服务端输出检查的静态保护文本；不含用户变量，不得作为公开 API 返回。

    读取随包分发的五段原文，稳定去重；调用方负责输出匹配策略。不会读取密钥或运行数据。
    """
    return tuple(dict.fromkeys(
        body for task_id in available_tasks()
        for body in load(task_id).sections.values()))


def load_capabilities() -> dict[str, Any]:
    """读机读能力清单（`prompts/chat_capabilities.json`，S7.11）。

    它**不是** prompt 资产：不进 `available_tasks()`（只扫 `.md`），是「测试可读 + 模型可见」
    的单一来源——`chat_supervisor` 用 `{{ capabilities }}` 把它注入 §02 Task，评测脚本按
    `category` 归类越界用例。格式不合契约直接抛 `PromptError`，不静默降级。
    """
    path = resources.files(PACKAGE).joinpath(CAPABILITIES_NAME)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptError(f"未找到能力清单：{CAPABILITIES_NAME}") from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise PromptError(f"能力清单不是合法 JSON（{exc}）") from exc
    if not isinstance(payload, dict):
        raise PromptError("能力清单的顶层必须是 JSON 对象")
    categories = payload.get("categories")
    if not isinstance(categories, list) or not categories:
        raise PromptError("能力清单缺少 categories 数组")
    ids = [str(item.get("id") or "") for item in categories if isinstance(item, dict)]
    if tuple(ids) != CAPABILITY_CATEGORIES:
        raise PromptError(
            f"能力清单的三类边界必须逐项为 {list(CAPABILITY_CATEGORIES)}，实际是 {ids}")
    for item in categories:
        for key in ("title", "judge", "reply"):
            if not str(item.get(key) or "").strip():
                raise PromptError(f"能力清单的 {item.get('id')} 缺少 {key}")
    return payload


def render_capabilities(payload: dict[str, Any] | None = None, *,
                        web_enabled: bool = False) -> str:
    """把能力清单渲染成注入 §02 Task 的确定性文本（无模板引擎）。

    `web_enabled`（Task 9）只在本轮**下发了网页工具**时为真：标题改成能涵盖网页能力的措辞，
    并在三类边界之后追加 `web_clause`——它是叠加在「能力内」之上的一项额外能力，**不新增
    第四类边界**（编号恒为三项）。关闭态与「同一份清单去掉 `web_clause`」逐字相等，除版本号
    外不留任何网页工具痕迹；标题也保持原样。
    """
    data = payload if payload is not None else load_capabilities()
    clause = data.get("web_clause")
    if not (web_enabled and isinstance(clause, dict)):
        clause = None
    suffix = " + 网页能力" if clause else ""
    lines = [f"【能力清单 v{data.get('version', '')} · 三类边界{suffix}】"]
    for index, item in enumerate(data.get("categories") or [], start=1):
        lines.append(f"{index}. {item['title']} —— 判定：{item['judge']}")
        lines.append(f"   固定话术：{item['reply']}")
    if clause:
        lines.append("附加能力（叠加在「能力内」上，不是第四类边界；本轮下发了网页工具才生效）"
                     f"—— 判定：{clause.get('judge_append', '')}")
        lines.append(f"   固定话术：{clause.get('reply_append', '')}")
    return "\n".join(lines)
