"""契约一致性校验：FastAPI 导出的 OpenAPI 必须覆盖 `docs/contracts/openapi.yaml`（S4.6）。

用法：

    uv run python scripts/check_openapi.py             # 退出码 0 = 一致，1 = 有差异
    uv run python scripts/check_openapi.py --verbose   # 连同"比对过但一致"的项一起打印

比对口径是**契约 ⊆ 实现**（契约是下界，实现可以更丰富）：

- paths 与每个 path 的 methods 集合必须相等；
- 每个操作的参数（`name`/`in`/`required` 与 schema 的 `type`/`format`/`enum`）、请求体
  （content-type 集合 + schema 形状）、契约声明的每个响应状态码与响应内容
  （content-type 集合 + schema 形状）、契约声明的响应头，都必须被实现覆盖；
- 实现多出来的状态码只允许白名单里的（当前只有 FastAPI 参数校验自动生成的 `422`）；
- schema 形状递归比对 `type`/`format`/`enum`/`properties`/`required`/`items`
  与 `additionalProperties: false`；契约引用到的 `components.schemas` 必须存在
  （实现多出来的 `HTTPValidationError` / `ValidationError` 是 FastAPI 自动加的，忽略）。

**有意不比对**：`info` / `servers` / `tags` 文案、自动生成的 `operationId`、数值与长度约束
（`minimum` / `maximum` / `minItems` / `maxLength`…）。这类约束由 domain 层的 Pydantic 校验器
与运行期强制（见 `src/xhs_agent/schemas.py` 与 `tests/integration/test_api_endpoints.py`），
OpenAPI 这边只保证**结构**一致——同一件事不在两处维护，差异信息也更可读。

CI 不单独加 step：`tests/unit/test_openapi_contract.py` 会调用本模块的 `check()`，
它天然跑在 `test` job 的 pytest 里，本地 pre-commit 也会拦住。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from xhs_agent.api.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "docs" / "contracts" / "openapi.yaml"

METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")
# 实现侧允许比契约多出来的状态码：FastAPI 参数校验自动生成的 422
EXTRA_STATUS_WHITELIST = {"422"}
# schema 形状里参与比对的键（数值/长度约束有意排除，见模块 docstring）
SHAPE_KEYS = ("type", "format", "enum", "items", "additionalProperties")
SCHEMA_REF_PREFIX = "#/components/schemas/"


def load_contract(path: Path | None = None) -> dict[str, Any]:
    text = (path or CONTRACT_PATH).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


def deref(doc: dict[str, Any], node: Any, *, where: str = "") -> Any:
    """把 `$ref` 解到实体；循环引用直接报错（契约里不该出现）。"""
    seen = 0
    while isinstance(node, dict) and "$ref" in node:
        ref = str(node["$ref"])
        if not ref.startswith("#/"):
            raise AssertionError(f"不支持外部 $ref：{ref}（{where}）")
        target: Any = doc
        for part in ref[2:].split("/"):
            if not isinstance(target, dict) or part not in target:
                raise MissingRefError(ref, where)
            target = target[part]
        node = target
        seen += 1
        if seen > 20:
            raise AssertionError(f"$ref 循环：{ref}（{where}）")
    return node


class MissingRefError(RuntimeError):
    """`$ref` 指向的组件不存在（例如实现把某个 schema 删了）。"""

    def __init__(self, ref: str, where: str) -> None:
        super().__init__(f"{where}：$ref 指向的组件不存在（{ref}）")
        self.ref = ref


def schema_shape(doc: dict[str, Any], node: Any, *, where: str) -> dict[str, Any]:
    """把 schema 归一化成可比较的形状（忽略 title/description/example/数值约束）。"""
    node = deref(doc, node, where=where)
    if not isinstance(node, dict):
        return {"type": repr(node)}
    shape: dict[str, Any] = {}
    for key in SHAPE_KEYS:
        if key in node:
            shape[key] = node[key]
    # `T | null` 归一成 `T`：可空性由运行时与 domain 层保证，这边只比结构
    # （契约有些写 `type: integer`、有些写 `oneOf: [X, null]`，归一后两边才可比）。
    if "anyOf" in node or "oneOf" in node:
        members = [schema_shape(doc, item, where=f"{where}/union")
                   for item in (node.get("anyOf") or node.get("oneOf") or [])
                   if item != {"type": "null"}]
        if len(members) == 1:
            shape.update(members[0])
        elif members:
            shape["union"] = members
    if "allOf" in node:
        shape["allOf"] = [schema_shape(doc, item, where=f"{where}/allOf")
                          for item in node["allOf"]]
    if "properties" in node:
        shape["properties"] = {name: schema_shape(doc, value, where=f"{where}/{name}")
                               for name, value in node["properties"].items()}
    if "items" in node:
        # 递归进 items：否则嵌套对象两边都是同名 `$ref`、"永远相等"，直接漏检
        shape["items"] = schema_shape(doc, node["items"], where=f"{where}[]")
    if "required" in node:
        shape["required"] = sorted(node["required"])
    return shape


def _compare_schema(contract_doc: dict[str, Any], impl_doc: dict[str, Any], contract_node: Any,
                    impl_node: Any, where: str, diffs: list[str],
                    notes: list[str]) -> None:
    """契约侧的形状必须被实现侧覆盖（逐键比对，缺失/取值不同都算差异）。"""
    try:
        left = schema_shape(contract_doc, contract_node, where=where)
    except MissingRefError as exc:   # 契约自身的问题：直接报出来
        diffs.append(str(exc))
        return
    try:
        right = schema_shape(impl_doc, impl_node, where=where)
    except MissingRefError as exc:   # 实现少了被引用的组件
        diffs.append(str(exc))
        return
    _compare_shape(left, right, where, diffs)
    notes.append(f"OK   {where}")


def _compare_shape(left: dict[str, Any], right: dict[str, Any], where: str,
                   diffs: list[str]) -> None:
    """递归比对两个归一化形状：契约侧出现的键必须在实现侧存在且兼容。"""
    for key, expected in left.items():
        if key not in right:
            diffs.append(f"{where}：契约声明了 {key}={expected!r}，实现没有")
            continue
        actual = right[key]
        if key == "properties":
            for name, prop in expected.items():
                if name not in actual:
                    diffs.append(f"{where}.properties：契约有属性 {name!r}，实现没有")
                else:
                    _compare_shape(prop, actual[name], f"{where}.{name}", diffs)
            continue
        if key == "required":
            missing = sorted(set(expected) - set(actual))
            if missing:
                diffs.append(f"{where}.required：实现少了必填项 {missing}")
            continue
        if key == "items":
            if not isinstance(actual, dict):
                diffs.append(f"{where}.items：契约 {expected!r} ≠ 实现 {actual!r}")
            else:
                _compare_shape(expected, actual, f"{where}[]", diffs)
            continue
        if key == "union":
            # 两边都归一过；要求契约的每个候选都能在实现里找到（顺序无关）
            expected_members = sorted(json.dumps(item, sort_keys=True, ensure_ascii=False)
                                      for item in expected)
            actual_members = sorted(json.dumps(item, sort_keys=True, ensure_ascii=False)
                                    for item in (actual if isinstance(actual, list) else [actual]))
            missing = [item for item in expected_members if item not in actual_members]
            if missing:
                diffs.append(f"{where}.union：契约有而实现没有的候选 {missing}")
            continue
        if key == "allOf" and left[key] != right[key]:
            diffs.append(f"{where}.allOf：契约 {expected!r} ≠ 实现 {actual!r}")
            continue
        if expected != actual:
            diffs.append(f"{where}：契约 {key}={expected!r} ≠ 实现 {key}={actual!r}")


def _compare_parameters(contract_doc: dict[str, Any], impl_doc: dict[str, Any],
                        contract_op: dict[str, Any], impl_op: dict[str, Any],
                        where: str, diffs: list[str], notes: list[str]) -> None:
    def normalize(doc: dict[str, Any], op: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in op.get("parameters") or []:
            param = deref(doc, raw, where=where)
            out[(str(param.get("name")), str(param.get("in")))] = param
        return out

    expected = normalize(contract_doc, contract_op)
    actual = normalize(impl_doc, impl_op)
    for key, param in expected.items():
        label = f"{where} 参数 {key[1]}:{key[0]}"
        if key not in actual:
            diffs.append(f"{label}：实现没有声明这个参数")
            continue
        got = actual[key]
        if bool(param.get("required")) != bool(got.get("required")):
            diffs.append(f"{label}：required 契约 {bool(param.get('required'))} ≠ "
                         f"实现 {bool(got.get('required'))}")
        _compare_schema(contract_doc, impl_doc, param.get("schema") or {},
                        got.get("schema") or {}, label, diffs, notes)


def _compare_responses(contract_doc: dict[str, Any], impl_doc: dict[str, Any],
                       contract_op: dict[str, Any], impl_op: dict[str, Any],
                       where: str, diffs: list[str], notes: list[str]) -> None:
    expected = contract_op.get("responses") or {}
    actual = impl_op.get("responses") or {}
    extra = sorted(set(actual) - set(expected))
    unexpected = [code for code in extra if code not in EXTRA_STATUS_WHITELIST]
    if unexpected:
        diffs.append(f"{where} 响应：实现多出契约没有的状态码 {unexpected}"
                     f"（白名单只允许 {sorted(EXTRA_STATUS_WHITELIST)}）")
    for code, response in expected.items():
        label = f"{where} 响应 {code}"
        if code not in actual:
            diffs.append(f"{label}：实现没有声明这个状态码")
            continue
        got = deref(impl_doc, actual[code], where=label)
        contract_response = deref(contract_doc, response, where=label)
        for header in (contract_response.get("headers") or {}):
            if header not in (got.get("headers") or {}):
                diffs.append(f"{label}：契约声明了响应头 {header!r}，实现没有")
        expected_content = contract_response.get("content") or {}
        actual_content = got.get("content") or {}
        for media_type, media in expected_content.items():
            media_label = f"{label} {media_type}"
            if media_type not in actual_content:
                diffs.append(f"{media_label}：实现没有声明这个 content-type")
                continue
            _compare_schema(contract_doc, impl_doc, media.get("schema") or {},
                            actual_content[media_type].get("schema") or {},
                            media_label, diffs, notes)


def _compare_request_body(contract_doc: dict[str, Any], impl_doc: dict[str, Any],
                          contract_op: dict[str, Any], impl_op: dict[str, Any],
                          where: str, diffs: list[str], notes: list[str]) -> None:
    expected = deref(contract_doc, contract_op.get("requestBody") or {}, where=where)
    if not expected:
        return
    actual = deref(impl_doc, impl_op.get("requestBody") or {}, where=where)
    label = f"{where} 请求体"
    if not actual:
        diffs.append(f"{label}：实现没有声明请求体")
        return
    if bool(expected.get("required")) != bool(actual.get("required")):
        diffs.append(f"{label}：required 契约 {bool(expected.get('required'))} ≠ "
                     f"实现 {bool(actual.get('required'))}")
    for media_type, media in (expected.get("content") or {}).items():
        media_label = f"{label} {media_type}"
        if media_type not in (actual.get("content") or {}):
            diffs.append(f"{media_label}：实现没有声明这个 content-type")
            continue
        _compare_schema(contract_doc, impl_doc, media.get("schema") or {},
                        actual["content"][media_type].get("schema") or {},
                        media_label, diffs, notes)


def compare(impl: dict[str, Any], contract: dict[str, Any],
            *, verbose: bool = False) -> list[str]:
    """返回差异清单（空列表 = 一致）；`verbose` 会把"比对过且一致"的项也打出来。"""
    diffs: list[str] = []
    notes: list[str] = []

    contract_paths = contract.get("paths") or {}
    impl_paths = impl.get("paths") or {}
    if set(contract_paths) != set(impl_paths):
        diffs.append(f"paths 不一致：契约有而实现没有 {sorted(set(contract_paths) - set(impl_paths))}；"
                     f"实现有而契约没有 {sorted(set(impl_paths) - set(contract_paths))}")
    for path in sorted(set(contract_paths) & set(impl_paths)):
        contract_methods = {m for m in contract_paths[path] if m in METHODS}
        impl_methods = {m for m in impl_paths[path] if m in METHODS}
        if contract_methods != impl_methods:
            diffs.append(f"{path}：methods 契约 {sorted(contract_methods)} ≠ "
                         f"实现 {sorted(impl_methods)}")
        for method in sorted(contract_methods & impl_methods):
            where = f"{method.upper()} {path}"
            contract_op = contract_paths[path][method]
            impl_op = impl_paths[path][method]
            _compare_parameters(contract, impl, contract_op, impl_op, where, diffs, notes)
            _compare_request_body(contract, impl, contract_op, impl_op, where, diffs, notes)
            _compare_responses(contract, impl, contract_op, impl_op, where, diffs, notes)
            notes.append(f"OK   {where}")

    contract_schemas = set((contract.get("components") or {}).get("schemas") or {})
    impl_schemas = set((impl.get("components") or {}).get("schemas") or {})
    missing = sorted(_referenced_schemas(contract) - impl_schemas)
    if missing:
        diffs.append(f"components.schemas：契约引用到但实现没有的 schema {missing}")
    notes.append(f"OK   components.schemas（契约 {len(contract_schemas)} 个，"
                 f"实现 {len(impl_schemas)} 个，实现多出的忽略）")

    if verbose:
        print("\n".join(sorted(notes)))
    return diffs


def _referenced_schemas(contract: dict[str, Any]) -> set[str]:
    """契约里被 `$ref` 引用到的 schema 名（含互相引用）。"""
    found: set[str] = set()
    stack: list[Any] = [contract]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith(SCHEMA_REF_PREFIX):
                found.add(ref[len(SCHEMA_REF_PREFIX):])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def check(*, verbose: bool = False, contract: dict[str, Any] | None = None) -> list[str]:
    """把当前应用导出的 OpenAPI 与契约比对，返回差异清单。"""
    return compare(create_app().openapi(), contract or load_contract(), verbose=verbose)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/check_openapi.py",
                                     description="校验 FastAPI 导出的 OpenAPI 是否覆盖契约（S4.6）")
    parser.add_argument("--verbose", action="store_true", help="连一致项一起打印")
    args = parser.parse_args(argv)
    diffs = check(verbose=args.verbose)
    if not diffs:
        print("契约一致性校验通过：FastAPI 导出的 OpenAPI 覆盖 docs/contracts/openapi.yaml")
        return 0
    print(f"契约一致性校验失败：{len(diffs)} 处差异（契约是事实源，改实现或先改契约）")
    for item in diffs:
        print(f"  - {item}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
