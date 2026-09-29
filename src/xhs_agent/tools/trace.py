"""运行态记录：每一步的状态、耗时、token、成本，以及产物落盘。

对应十步路线里的「Context / Memory」和「部署与监控」：
- state.json：这次任务执行到哪一步、哪些还没做、用的什么版本
- trace.jsonl：逐条事件流，便于排查和统计
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager

from ..util import append_jsonl, now_iso, slugify, truncate, write_json, write_text


class RunStore:
    def __init__(self, runs_dir: str, slug: str, meta: dict | None = None) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{stamp}-{slugify(slug, 20, 'run')}"
        self.dir = os.path.join(runs_dir, self.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self.trace_path = os.path.join(self.dir, "trace.jsonl")
        self.state = {
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
        self._step_stack: list = []
        self.log("run_started", {"run_id": self.run_id})

    # ---------- 步骤 ----------
    @contextmanager
    def step(self, name: str, detail: str = ""):
        record = {
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
            record["status"] = "succeeded"
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
            self.log("step_failed", {"name": name, "error": record["error"]})
            raise
        finally:
            record["ended_at"] = now_iso()
            record["latency_ms"] = int((time.time() - started) * 1000)
            self._step_stack.pop()
            if record["status"] == "running":
                record["status"] = "succeeded"
            self.state["totals"]["latency_ms"] += record["latency_ms"]
            self.log("step_finished", {"name": name, "status": record["status"],
                                       "latency_ms": record["latency_ms"]})
            self.save_state()

    # ---------- 记录 ----------
    def record_llm(self, record: dict) -> None:
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

    def log(self, event: str, payload: dict | None = None) -> None:
        append_jsonl(self.trace_path, {"ts": now_iso(), "event": event, "payload": payload or {}})

    # ---------- 落盘 ----------
    def save_json(self, name: str, obj) -> str:
        path = os.path.join(self.dir, name)
        write_json(path, obj)
        self._register_artifact(name)
        return path

    def save_text(self, name: str, text: str) -> str:
        path = os.path.join(self.dir, name)
        write_text(path, text)
        self._register_artifact(name)
        return path

    def save_state(self) -> str:
        path = os.path.join(self.dir, "state.json")
        self.state["pending"] = [s["name"] for s in self.state.get("planned_steps", []) if s not in
                                 [step["name"] for step in self.state["steps"]]]
        write_json(path, self.state)
        return path

    def _register_artifact(self, name: str) -> None:
        if name not in self.state["artifacts"]:
            self.state["artifacts"].append(name)
        if self._step_stack and name not in self._step_stack[-1]["artifacts"]:
            self._step_stack[-1]["artifacts"].append(name)

    def plan(self, steps: list) -> None:
        self.state["planned_steps"] = list(steps)

    def finalize(self, status: str = "succeeded", notes: str = "") -> str:
        self.state["status"] = status
        self.state["finished_at"] = now_iso()
        self.state["notes"] = truncate(notes, 400, suffix="")
        self.state["totals"]["cost_cny"] = round(self.state["totals"]["cost_cny"], 4)
        self.log("run_finished", {"status": status, "totals": self.state["totals"]})
        return self.save_state()

    def summary_lines(self) -> list:
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