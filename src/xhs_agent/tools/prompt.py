"""Prompt 资产加载与渲染（prompt 契约 §三 / §四）。

- 资源位置：`src/xhs_agent/prompts/<task_id>.md`，随 wheel 分发，用 importlib.resources 读取。
- 五段：01 System / 02 Task / 03 Constraints / 04 Examples / 05 Output Schema。
- 组装：system = 01 System + 03 Constraints；user = 02 Task（渲染后）+ 04 Examples + 05 Output Schema。
- 占位符 `{{name}}` **只在 02 Task 段生效**，其它段里出现的按字面文本处理。
- 未知 task_id / 缺段 / 缺变量 / requires 与实际占位符不符，一律抛 PromptError——
  不做静默降级（AGENTS.md 第 4 节，docs/contracts/prompt契约.md §四）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

PACKAGE = "xhs_agent.prompts"
SUFFIX = ".md"

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
            (self.sections["system"], self.sections["constraints"]))
        user = SECTION_SEPARATOR.join(
            (task_body, self.sections["examples"], self.sections["output_schema"]))
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