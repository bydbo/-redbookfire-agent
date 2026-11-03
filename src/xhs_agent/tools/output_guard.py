"""检查模型输出中的内部提示词片段，无网络或持久化副作用。

在正文、结构化结果和工具参数离开模型适配器之前使用；流式必须先完成检查再释放。
这是一层确定性的原文防护，不承诺识别任意转述、翻译或编码。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
from functools import lru_cache
from typing import Any

from . import prompt

_WINDOW = 48
_FENCES = re.compile(r"```.*?```", re.DOTALL)
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


class UnsafeModelOutput(ValueError):
    """内部内容未通过输出检查；异常文案不得附带命中的片段。"""

    def __init__(self) -> None:
        super().__init__("模型输出未通过安全检查，请重新描述业务需求后重试。")


def _normalise(text: str) -> str:
    text = _UNICODE_ESCAPE.sub(lambda match: chr(int(match[1], 16)), text)
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold()
                   if char.isalnum())


def _windows(text: str) -> set[str]:
    return {text[i:i + _WINDOW] for i in range(max(0, len(text) - _WINDOW + 1))}


def _prose(text: str) -> str:
    # JSON 字段表本来就可能出现在合法结构化响应中，不据此判为泄露。
    return _FENCES.sub("", text)


def _decoded_texts(value: Any, depth: int = 0, *, keys: bool = True) -> list[str]:
    """检查常见编码与 JSON 字符串叶子；有界递归，避免转义换行和拆字段绕过。"""
    if depth > 6:
        return []
    if isinstance(value, dict):
        items = [item for pair in value.items() for item in pair] if keys else list(value.values())
        return [piece for item in items for piece in _decoded_texts(item, depth + 1, keys=keys)]
    if isinstance(value, list):
        return [piece for item in value for piece in _decoded_texts(item, depth + 1, keys=keys)]
    if not isinstance(value, str):
        return []
    result = [value]
    try:
        decoded = json.loads(value)
    except (ValueError, RecursionError):
        pass
    else:
        if decoded != value:
            result.extend(_decoded_texts(decoded, depth + 1, keys=keys))
    for match in re.finditer(r"[A-Za-z0-9+/_-]{64,}={0,2}", value[:100_000]):
        token = match[0]
        try:
            decoded_text = base64.b64decode(token + "=" * (-len(token) % 4),
                                           altchars=b"-_", validate=True).decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error):
            continue
        if decoded_text != value:
            result.extend(_decoded_texts(decoded_text, depth + 1, keys=keys))
    return result


@lru_cache(maxsize=1)
def _static_windows() -> frozenset[str]:
    sources = [
        asset.sections[key]
        for task in prompt.available_tasks()
        for asset in [prompt.load(task)]
        for key in ("system", "constraints", "examples", "output_schema")
    ]
    protected: set[str] = set()
    for source in sources:
        protected.update(_windows(_normalise(_prose(source))))
    for task in prompt.available_tasks():
        examples = prompt.load(task).sections["examples"]
        for block in re.findall(r"```(?:json)?\s*\n(.*?)```", examples, re.DOTALL):
            # 保护具体示例值，保留 schema 字段名相同但值不同的合法业务输出。
            try:
                example = json.loads(block)
            except ValueError:
                continue
            protected.update(_windows(_normalise("\n".join(_decoded_texts(example, keys=False)))))
    # 产品公开的能力说明/拒绝话术允许正常回复，不应因其也在提示词里而误拦。
    protected.difference_update(_public_windows())
    return frozenset(protected)


@lru_cache(maxsize=1)
def _public_windows() -> frozenset[str]:
    payload = prompt.load_capabilities()
    replies = [str(category["reply"]) for category in payload["categories"]]
    # 清单里凡是要求模型「照原样说」的槽都算公开话术：开启态的网页能力段落同样要原样说出来，
    # 不纳入白名单就会「清单让它说、这里转头把它拦掉」。它是静态资产，关闭态不会渲染，
    # 多说一句可公开话术没有副作用。
    clause = payload.get("web_clause")
    if isinstance(clause, dict) and str(clause.get("reply_append") or "").strip():
        replies.append(str(clause["reply_append"]))
    # 只有能力介绍范例明确属于公开回答；带假想素材身份的其他范例仍保护。
    example = prompt.load("chat_supervisor").sections["examples"].split("例 2", 1)[0]
    if "- 你：" in example:
        replies.append(example.split("- 你：", 1)[1].strip())
    return frozenset(part for reply in replies for part in _windows(_normalise(reply)))


def ensure_safe_output(text: str, *, system: str = "",
                       tool_calls: list[dict[str, Any]] | None = None) -> None:
    """发现内部规则的连续长片段即拒绝整份输出，包括工具参数；不返回原文。"""
    protected = set(_static_windows())
    protected.update(_windows(_normalise(_prose(system))))
    protected.difference_update(_public_windows())
    outputs = _decoded_texts(text)
    if tool_calls:
        outputs.extend(_decoded_texts(tool_calls))
    outputs.append("\n".join(outputs))
    outputs.append("\n".join(_decoded_texts(text, keys=False)))
    if tool_calls:
        outputs.append("\n".join(_decoded_texts(tool_calls, keys=False)))
    for output in outputs:
        if len(re.findall(r"##\s+0[1345]\s+(?:System|Constraints|Examples|Output Schema)",
                          output, flags=re.IGNORECASE)) >= 2:
            raise UnsafeModelOutput()
        normalised = _normalise(output)
        if any(normalised[i:i + _WINDOW] in protected
               for i in range(max(0, len(normalised) - _WINDOW + 1))):
            raise UnsafeModelOutput()
