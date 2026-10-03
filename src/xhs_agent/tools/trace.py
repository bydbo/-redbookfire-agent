"""运行态记录：进度事件流与产物登记（ADR 0011）。

用途：把一次运行的步骤、耗时、token、成本记在内存里，并落成 `trace.jsonl`（逐条事件流）
      与各步骤产物。运行**状态**的权威源是数据库的 `runs` / `run_hotspots` / `run_matches`
      （ADR 0011），因此本模块**不再落 `state.json`**。
输入：`runs_dir` + `slug`（可选 `meta` / `run_id`）；运行中调用 `step` / `record_llm` / `save_*`。
输出：`runs/<run_id>/` 目录，内含 `trace.jsonl` 与各步骤产物；`finalize()` 返回运行目录。
      `run_id` 传入时（worker 传 DB 的 run_id）用它作目录名，否则按 `时间戳-slug` 生成。
      时间戳来自系统时钟——运行记录本身就是要记时间。

对应十步路线里的「Context / Memory」和「部署与监控」：
- trace.jsonl：逐条事件流，便于排查和统计
- 内存 `state`：本次运行累计的步骤与 totals（进程内用；状态以数据库为准）
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ..util import append_jsonl, now_iso, slugify, truncate, write_json, write_text


class RunStore:
    """一次运行的产物目录与事件流；运行状态不落盘（ADR 0011）。"""

    def __init__(self, runs_dir: str, slug: str, meta: dict[str, Any] | None = None,
                 run_id: str | None = None) -> None:
        if run_id:
            self.run_id = str(run_id)
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.run_id = f"{stamp}-{slugify(slug, 20, 'run')}"
        self.dir = os.path.join(runs_dir, self.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self.trace_path = os.path.join(self.dir, "trace.jsonl")
        self.state: dict[str, Any] = {
            "run_id": self.run_id,
            "created_at": now_iso(),
            "finished_at": "",
            "status": "running",
            "meta": meta or {},
            "steps": [],
            "artifacts": [],
            "pending": [],
            "totals": {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                       "cost_cny": 0.0, "latency_ms": 0},
            "notes": "",
        }
        self._step_stack: list[dict[str, Any]] = []
        self.log("run_started", {"run_id": self.run_id})

    # ---------- 步骤 ----------
    @contextmanager
    def step(self, name: str, detail: str = "") -> Iterator[dict[str, Any]]:
        record: dict[str, Any] = {
            "name": name,
            "detail": detail,
            "status": "running",
            "started_at": now_iso(),
            "ended_at": "",
            "latency_ms": 0,
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_cny": 0.0,
            "artifacts": [],
            "error": "",
        }
        self.state["steps"].append(record)
        self._step_stack.append(record)
        started = time.time()
        self.log("step_started", {"name": name, "detail": detail})
        try:
            yield record
        except BaseException as exc:
            # 必须捕 BaseException：KeyboardInterrupt / SystemExit 若不在此标记为失败，
            # finally 里的回填会把中断的步骤写成 succeeded，运行记录失真。
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
            self.log("step_failed", {"name": name, "error": record["error"]})
            raise
        else:
            record["status"] = "succeeded"
        finally:
            record["ended_at"] = now_iso()
            record["latency_ms"] = int((time.time() - started) * 1000)
            self._step_stack.pop()
            self.state["totals"]["latency_ms"] += record["latency_ms"]
            self.log("step_finished", {"name": name, "status": record["status"],
                                       "latency_ms": record["latency_ms"]})

    # ---------- 记录 ----------
    def record_llm(self, record: dict[str, Any]) -> None:
        self.state["totals"]["llm_calls"] += 1
        self.state["totals"]["prompt_tokens"] += int(record.get("prompt_tokens") or 0)
        self.state["totals"]["completion_tokens"] += int(record.get("completion_tokens") or 0)
        self.state["totals"]["cost_cny"] += float(record.get("cost_cny") or 0)
        if self._step_stack:
            step = self._step_stack[-1]
            step["llm_calls"] += 1
            step["prompt_tokens"] += int(record.get("prompt_tokens") or 0)
            step["completion_tokens"] += int(record.get("completion_tokens") or 0)
            step["cost_cny"] += float(record.get("cost_cny") or 0)
        self.log("llm_call", record)

    def log(self, event: str, payload: dict[str, Any] | None = None) -> None:
        append_jsonl(self.trace_path, {"ts": now_iso(), "event": event, "payload": payload or {}})

    # ---------- 落盘 ----------
    def save_json(self, name: str, obj: Any) -> str:
        path = os.path.join(self.dir, name)
        write_json(path, obj)
        self._register_artifact(name)
        return path

    def save_text(self, name: str, text: str) -> str:
        path = os.path.join(self.dir, name)
        write_text(path, text)
        self._register_artifact(name)
        return path

    def pending_steps(self) -> list[str]:
        """计划了但还没跑的步骤名（纯内存计算；不再落 `state.json`）。"""
        done = {step["name"] for step in self.state["steps"]}
        pending = [step["name"] for step in self.state.get("planned_steps", [])
                   if step["name"] not in done]
        self.state["pending"] = pending
        return pending

    def _register_artifact(self, name: str) -> None:
        if name not in self.state["artifacts"]:
            self.state["artifacts"].append(name)
        if self._step_stack and name not in self._step_stack[-1]["artifacts"]:
            self._step_stack[-1]["artifacts"].append(name)

    def plan(self, steps: list[Any]) -> None:
        """登记计划步骤，用于 `pending_steps()` 计算。

        输入：步骤列表，元素可以是字符串（`"拆解"`）或 `{"name": "拆解"}`；输出：无。
        内部统一存成 `{"name": ...}`——`pending_steps()` 按这个形状读取（S1.4 单测发现的崩溃点）。
        """
        self.state["planned_steps"] = [
            {"name": item} if isinstance(item, str) else dict(item) for item in steps
        ]

    def finalize(self, status: str = "succeeded", notes: str = "") -> str:
        """收尾：更新内存状态并追加 `run_finished` 事件，返回运行目录路径。"""
        self.state["status"] = status
        self.state["finished_at"] = now_iso()
        self.state["notes"] = truncate(notes, 400, suffix="")
        self.state["totals"]["cost_cny"] = round(self.state["totals"]["cost_cny"], 4)
        self.pending_steps()
        self.log("run_finished", {"status": status, "totals": self.state["totals"]})
        return self.dir

    def summary_lines(self) -> list[str]:
        totals = self.state["totals"]
        lines = [
            f"- 运行 ID：`{self.run_id}`",
            f"- 状态：{self.state['status']}",
            f"- 步骤：{len(self.state['steps'])} 个（失败 "
            f"{sum(1 for s in self.state['steps'] if s['status'] == 'failed')} 个）",
            f"- 模型调用：{totals['llm_calls']} 次，输入 {totals['prompt_tokens']} / 输出 "
            f"{totals['completion_tokens']} tokens，预估成本 {totals['cost_cny']:.4f} 元",
            f"- 总耗时：{totals['latency_ms'] / 1000:.1f} 秒",
        ]
        return lines
