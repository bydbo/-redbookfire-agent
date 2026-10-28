"""E9 数据包加载与冻结校验。

用途：把 `evals/e9-v1/`（或临时工程夹具）读成结构化 `Dataset`，在付费运行前检查样本、
分区、标注、hash、授权和 E2E 子集。校验只读，不修改数据；冻结由 manifest 状态控制。
输入：实验目录（含 `manifest.json` 等固定文件）。
输出：`Dataset` 与 `ValidationReport`；错误按退出码 3 的语义聚合，不抛业务异常。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import (
    INPUT_TYPES,
    SCENARIO_TAGS,
    Case,
    CaseReview,
    Dataset,
    DatasetManifest,
    E2ETask,
    MaterialAsset,
    Qrel,
)

CORE_FILES = (
    "materials.json",
    "cases.json",
    "qrels.json",
    "e2e.json",
    "authorizations.json",
)
EXPECTED_CASES: dict[str, int] = {
    "text": 20,
    "single_image": 10,
    "multi_same_topic": 10,
    "multi_mixed": 10,
}
EXPECTED_SPLIT = {"dev": 20, "holdout": 30}
EXPECTED_UNANSWERABLE = {"dev": 4, "holdout": 6}
E2E_INPUT_COUNTS = {
    "text": 6,
    "single_image": 4,
    "multi_same_topic": 5,
    "multi_mixed": 5,
}
SCENARIO_MIN = 5


@dataclass
class ValidationReport:
    """一次校验的结果；`errors` 非空即不可冻结 / 不可 live。"""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_list(path: Path, key: str, model: type[Any]) -> list[Any]:
    data = _read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise ValueError(f"{path.name} 必须是包含 `{key}` 数组的对象")
    return [model.model_validate(item) for item in data[key]]


def load_dataset(root: Path) -> Dataset:
    """读取固定文件；结构错误抛 `ValueError`（由 CLI 折成退出码 3）。"""
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"缺少 {manifest_path}")
    manifest = DatasetManifest.model_validate(_read_json(manifest_path))
    materials = _load_list(root / "materials.json", "materials", MaterialAsset)
    cases = _load_list(root / "cases.json", "cases", Case)
    qrels_path = _read_json(root / "qrels.json")
    qrels = [Qrel.model_validate(item) for item in qrels_path.get("qrels", [])]
    reviews = [CaseReview.model_validate(item) for item in qrels_path.get("reviews", [])]
    e2e_tasks = [E2ETask.model_validate(item)
                 for item in _read_json(root / "e2e.json").get("tasks", [])]
    auth = _read_json(root / "authorizations.json")
    authorizations = [str(item) for item in auth.get("material_keys", [])]
    return Dataset(
        manifest=manifest,
        materials=materials,
        cases=cases,
        qrels=qrels,
        reviews=reviews,
        e2e_tasks=e2e_tasks,
        authorizations=authorizations,
    )


def _unique(items: list[Any], attr: str, label: str, report: ValidationReport) -> None:
    seen: set[str] = set()
    for item in items:
        value = str(getattr(item, attr))
        if value in seen:
            report.add_error(f"{label} 重复：{value}")
        seen.add(value)


def _check_files(root: Path, dataset: Dataset, report: ValidationReport) -> None:
    manifest = dataset.manifest
    if manifest.status == "frozen":
        missing = [name for name in CORE_FILES if name not in manifest.files]
        if missing:
            report.add_error(f"冻结 manifest 缺少文件 hash：{missing}")
    for name, expected in manifest.files.items():
        path = root / name
        if not path.is_file():
            report.add_error(f"manifest 登记的文件不存在：{name}")
            continue
        actual = sha256_file(path)
        if actual != expected:
            report.add_error(f"文件 hash 不符：{name}")


def _check_assets(root: Path, dataset: Dataset, report: ValidationReport) -> None:
    _unique(dataset.materials, "material_key", "素材 key", report)
    _unique(dataset.materials, "relative_path", "素材路径", report)
    by_key = dataset.assets_by_key
    for asset in dataset.materials:
        path = root / asset.relative_path
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            report.add_error(f"素材路径越出实验目录：{asset.relative_path}")
            continue
        if not path.is_file():
            if dataset.manifest.status == "frozen":
                report.add_error(f"素材文件不存在：{asset.relative_path}")
            else:
                report.add_warning(f"草稿素材文件尚未就位：{asset.relative_path}")
            continue
        if sha256_file(path) != asset.sha256:
            report.add_error(f"素材 hash 不符：{asset.relative_path}")
    for case in dataset.cases:
        refs = [item.material_key for item in case.natural_input.images]
        if len(refs) != len(set(refs)):
            report.add_error(f"{case.case_id} 图片顺序中有重复素材")
        if case.input_type == "text" and refs:
            report.add_error(f"{case.case_id} 文本任务不应带图片")
        if case.input_type == "single_image" and len(refs) != 1:
            report.add_error(f"{case.case_id} 单图任务必须恰好 1 张图")
        if case.input_type in ("multi_same_topic", "multi_mixed") and len(refs) < 2:
            report.add_error(f"{case.case_id} 多图任务至少 2 张图")
        for ref in case.natural_input.images:
            found = by_key.get(ref.material_key)
            if found is None:
                report.add_error(f"{case.case_id} 引用不存在的素材：{ref.material_key}")
            elif found.sha256 != ref.sha256:
                report.add_error(f"{case.case_id} 图片 hash 与素材清单不符：{ref.material_key}")


def _check_cases(dataset: Dataset, report: ValidationReport) -> None:
    _unique(dataset.cases, "case_id", "case_id", report)
    groups: dict[str, set[str]] = defaultdict(set)
    for case in dataset.cases:
        groups[case.group_id].add(case.split)
        if case.input_type not in INPUT_TYPES:
            report.add_error(f"{case.case_id} 输入类型非法：{case.input_type}")
        if case.expectation.answerable and not case.oracle.elements:
            report.add_error(f"{case.case_id} 有答案任务缺少 oracle 要素")
    for group, splits in groups.items():
        if len(splits) > 1:
            report.add_error(f"分组 {group} 跨开发集 / 留出集：{sorted(splits)}")
    by_text: dict[str, set[str]] = defaultdict(set)
    for case in dataset.cases:
        if case.natural_input.text:
            by_text[case.natural_input.text].add(case.split)
    for case_ids_text, splits in by_text.items():
        if len(splits) > 1:
            report.add_warning(
                f"相同自然输入出现在不同分区，需确认不是同需求改写：{case_ids_text[:40]}")


def _check_qrels(dataset: Dataset, report: ValidationReport) -> None:
    _unique(dataset.reviews, "case_id", "全库审阅", report)
    cases = {case.case_id: case for case in dataset.cases}
    materials = dataset.assets_by_key
    review_by_case = {review.case_id: review for review in dataset.reviews}
    seen: set[tuple[str, str]] = set()
    positive: dict[str, int] = defaultdict(int)
    for qrel in dataset.qrels:
        key = (qrel.case_id, qrel.material_key)
        if key in seen:
            report.add_error(f"qrel 重复：{qrel.case_id} / {qrel.material_key}")
        seen.add(key)
        if qrel.case_id not in cases:
            report.add_error(f"qrel 引用不存在的 case：{qrel.case_id}")
        if qrel.material_key not in materials:
            report.add_error(f"qrel 引用不存在的素材：{qrel.material_key}")
        if dataset.manifest.status == "frozen" and not qrel.confirmed:
            report.add_error(f"qrel 尚未确认：{qrel.case_id} / {qrel.material_key}")
        if qrel.grade >= 1:
            positive[qrel.case_id] += 1
    for case in dataset.cases:
        review = review_by_case.get(case.case_id)
        if review is None:
            report.add_error(f"{case.case_id} 缺少全库审阅记录")
            continue
        if review.reviewed_materials != len(dataset.materials):
            report.add_error(
                f"{case.case_id} 审阅素材数 {review.reviewed_materials} "
                f"不等于库大小 {len(dataset.materials)}")
        if review.library_size != len(dataset.materials):
            report.add_error(
                f"{case.case_id} 审阅记录库大小 {review.library_size} "
                f"与当前库 {len(dataset.materials)} 不一致")
        if case.expectation.answerable and positive[case.case_id] == 0:
            report.add_error(f"{case.case_id} 标为有答案但没有 grade>=1 的 qrel")
        if not case.expectation.answerable and positive[case.case_id] > 0:
            report.add_error(f"{case.case_id} 标为无答案但存在 grade>=1 的 qrel")


def _check_counts(dataset: Dataset, report: ValidationReport) -> None:
    split_counts: Counter[str] = Counter(case.split for case in dataset.cases)
    input_counts: Counter[str] = Counter(case.input_type for case in dataset.cases)
    unanswerable: Counter[str] = Counter(
        case.split for case in dataset.cases if not case.expectation.answerable)
    counts = {
        "cases": len(dataset.cases),
        "materials": len(dataset.materials),
        "splits": dict(split_counts),
        "inputs": dict(input_counts),
        "unanswerable": dict(unanswerable),
    }
    report.counts = counts
    if dataset.manifest.status != "frozen":
        report.add_warning("manifest 仍为 draft；冻结前必须补足样本并由用户确认标注")
        return
    if dataset.manifest.dataset_kind != "e9":
        return
    if split_counts.get("dev", 0) != EXPECTED_SPLIT["dev"]:
        report.add_error(f"开发集应为 {EXPECTED_SPLIT['dev']}，实际 {split_counts.get('dev', 0)}")
    if split_counts.get("holdout", 0) != EXPECTED_SPLIT["holdout"]:
        report.add_error(
            f"留出集应为 {EXPECTED_SPLIT['holdout']}，实际 {split_counts.get('holdout', 0)}")
    for input_type, expected in EXPECTED_CASES.items():
        if input_counts.get(input_type, 0) != expected:
            report.add_error(f"{input_type} 应为 {expected}，实际 {input_counts.get(input_type, 0)}")
    for split, expected in EXPECTED_UNANSWERABLE.items():
        if unanswerable.get(split, 0) != expected:
            report.add_error(
                f"{split} 无答案任务应为 {expected}，实际 {unanswerable.get(split, 0)}")
    for tag in SCENARIO_TAGS:
        amount = sum(1 for case in dataset.cases if tag in case.scenario_tags)
        if amount < SCENARIO_MIN:
            report.add_error(f"场景标签 {tag} 仅 {amount} 个，至少 {SCENARIO_MIN} 个")


def _check_e2e(dataset: Dataset, report: ValidationReport) -> None:
    _unique(dataset.e2e_tasks, "case_id", "E2E 任务", report)
    cases = {case.case_id: case for case in dataset.cases}
    if dataset.manifest.status != "frozen":
        return
    if dataset.manifest.dataset_kind != "e9":
        return
    if len(dataset.e2e_tasks) != 20:
        report.add_error(f"E2E 唯一任务应为 20，实际 {len(dataset.e2e_tasks)}")
    repeated = [task for task in dataset.e2e_tasks if task.repeats == 2]
    if len(repeated) != 6:
        report.add_error(f"E2E 指定重复任务应为 6，实际 {len(repeated)}")
    input_counts: Counter[str] = Counter()
    repeated_inputs: set[str] = set()
    unanswerable_inputs: set[str] = set()
    for task in dataset.e2e_tasks:
        case = cases.get(task.case_id)
        if case is None:
            report.add_error(f"E2E 引用不存在的 case：{task.case_id}")
            continue
        if case.split != "holdout":
            report.add_error(f"E2E 任务必须来自留出集：{task.case_id}")
        input_counts[case.input_type] += 1
        if task.repeats == 2:
            repeated_inputs.add(case.input_type)
        if not case.expectation.answerable:
            unanswerable_inputs.add(case.input_type)
        if task.repeats == 2 and case.expectation.answerable and not (
                task.has_followup or task.has_rewrite):
            report.add_error(f"重复任务 {task.case_id} 需包含追问或改稿或为无答案")
    for input_type, expected in E2E_INPUT_COUNTS.items():
        if input_counts.get(input_type, 0) != expected:
            report.add_error(
                f"E2E {input_type} 应为 {expected}，实际 {input_counts.get(input_type, 0)}")
    if len(unanswerable_inputs) != 4:
        report.add_error("E2E 四类输入各需至少 1 个无答案任务")
    if len(repeated_inputs) != 4:
        report.add_error("6 个重复任务必须覆盖四类输入")
    if sum(1 for task in dataset.e2e_tasks if task.has_followup) < 5:
        report.add_error("E2E 带追问任务至少 5 个")
    if sum(1 for task in dataset.e2e_tasks if task.has_rewrite) < 5:
        report.add_error("E2E 带改稿任务至少 5 个")


def _check_authorizations(dataset: Dataset, report: ValidationReport) -> None:
    keys = set(dataset.authorizations)
    for asset in dataset.materials:
        if asset.model_transfer_allowed and asset.license:
            continue
        report.add_error(f"素材缺少模型传输授权：{asset.material_key}")
    if dataset.manifest.status == "frozen":
        missing = [asset.material_key for asset in dataset.materials
                   if asset.material_key not in keys]
        if missing:
            report.add_error(f"授权摘要缺少素材：{missing[:5]}")


def validate_dataset(dataset: Dataset, root: Path,
                     require_frozen: bool = False) -> ValidationReport:
    """执行全部只读校验；`require_frozen` 为真时 draft 也视为错误。"""
    report = ValidationReport()
    if require_frozen and dataset.manifest.status != "frozen":
        report.add_error("数据版本尚未冻结（manifest.status != frozen）")
    _check_files(root, dataset, report)
    _check_assets(root, dataset, report)
    _check_cases(dataset, report)
    _check_qrels(dataset, report)
    _check_counts(dataset, report)
    _check_e2e(dataset, report)
    _check_authorizations(dataset, report)
    if dataset.manifest.status == "frozen" and not dataset.manifest.frozen_at:
        report.add_error("冻结 manifest 缺少 frozen_at")
    return report


def load_and_validate(root: Path, require_frozen: bool = False) -> tuple[Dataset | None,
                                                                        ValidationReport]:
    """便利入口：结构错误也折进 report，不向 CLI 抛 traceback。"""
    try:
        dataset = load_dataset(root)
    except (OSError, ValueError, ValidationError) as exc:
        report = ValidationReport()
        report.add_error(f"数据包结构无效：{type(exc).__name__}: {exc}")
        return None, report
    return dataset, validate_dataset(dataset, root, require_frozen=require_frozen)
