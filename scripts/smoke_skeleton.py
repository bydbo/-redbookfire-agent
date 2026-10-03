"""Walking Skeleton 联调（运维/验收脚本，S3.6）。

用途：一条命令把「起依赖 → 起 worker/API → 投热点 → 轮询 → 取结果 → 取报告」整条关键路径
      真实跑一遍，并断言《backlog》第四节要求的硬条件：结果里**至少 1 条候选**、带命中要素
      与理由，报告是**完整 HTML**。产物另存 `evals/reports/skeleton-<日期>.html|md`。
输入：无参数即可（读 `AppConfig`）；可选 `--case`（默认 case-01，用它标注的热点原文）、
      `--api-url`（只驱动已起的 API，不自己起进程）、`--no-seed`（不往素材库种示例包）、
      `--keep-alive`（跑完不回收进程）、`--timeout`（等终态的上限秒数）。
输出：逐步骤打印结果，退出码 0 = 跑通、1 = 任何一步失败。

口径：
- **素材来源**：素材库为空时把冻结的示例素材包 `evals/fixtures/demo_pack` 复制进
  `[paths].materials_dir`（产品方案 §8.3 的"脱敏示例素材包"）；库非空就原样用，不动用户素材。
- **索引与向量不需要手工准备**：worker 的前置步骤会自己做增量同步与向量回填（S3.4b）。
- **真实调用**：走真实文本模型与向量服务（一个热点约 3 次 LLM 调用，成本约几分钱）。
- 跑完**保留**这次运行（库里可回看）与两份报告；只有自己起的进程会被回收。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import html
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from xhs_agent.config import AppConfig, ConfigError, load_config
from xhs_agent.probe import preflight

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = PROJECT_ROOT / "evals"
CASES_DIR = EVALS_DIR / "cases"
PACK_DIR = EVALS_DIR / "fixtures" / "demo_pack"
REPORTS_DIR = EVALS_DIR / "reports"
DEFAULT_CASE = "case-01"
TERMINAL_STATUSES = ("succeeded", "failed")


class SkeletonError(RuntimeError):
    """关键路径没跑通。"""


# ---------------------------------------------------------------------------
# 纯函数部分（可离线单测）
# ---------------------------------------------------------------------------


def load_hotspot(case_id: str = DEFAULT_CASE) -> str:
    """从评测用例读热点原文（默认 case-01：反面 + 一条 must_hit 素材，最稳的验收样本）。"""
    path = CASES_DIR / f"{case_id}.json"
    if not path.is_file():
        raise SkeletonError(f"评测用例不存在：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    hotspot = str(data.get("hotspot") or "").strip()
    if not hotspot:
        raise SkeletonError(f"用例缺少 hotspot 字段：{path}")
    return hotspot


def _visible_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [p for p in sorted(root.rglob("*")) if p.is_file() and not p.name.startswith(".")]


def seed_materials(materials_dir: str, pack_dir: Path = PACK_DIR) -> int:
    """素材库为空时把示例素材包复制进去；返回复制的文件数（0 = 没动）。

    "空" 的判定只看非隐藏的普通文件——`.gitkeep` 不算素材，也不会因此跳过种包。
    库非空时一律不碰（绝不动用户的素材）。
    """
    target = Path(materials_dir)
    target.mkdir(parents=True, exist_ok=True)
    if _visible_files(target):
        return 0
    copied = 0
    for src in sorted(pack_dir.rglob("*")):
        if not src.is_file() or src.name.startswith("."):
            continue
        dst = target / src.relative_to(pack_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied


def verify_run_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Walking Skeleton 的硬验收：至少 1 条候选，且首选带命中要素与理由；返回首选候选。"""
    hotspots = detail.get("hotspots") or []
    if not hotspots:
        raise SkeletonError("运行结果里没有热点")
    candidates = hotspots[0].get("candidates") or []
    if not candidates:
        raise SkeletonError(
            "运行结果没有候选素材——素材库是空的？先让脚本种示例包（去掉 --no-seed），"
            "或把素材放进 [paths].materials_dir")
    top = candidates[0]
    if not top.get("hits"):
        raise SkeletonError(f"首选候选缺少命中要素：{top.get('material_id')}")
    if not top.get("reasons"):
        raise SkeletonError(f"首选候选缺少理由：{top.get('material_id')}")
    return top


def verify_report(body: str, *, fmt: str, hotspot: str = "") -> None:
    """报告要"完整"：HTML 有头有尾、正文不短，且能看到热点原文（证明数据落全了）。"""
    if fmt == "html":
        lowered = body.lower()
        if "<html" not in lowered or "</html>" not in lowered:
            raise SkeletonError("报告 HTML 不完整：缺 <html> / </html>")
    if len(body) < 500:
        raise SkeletonError(f"报告内容过短（{len(body)} 字符），像是没渲染出真实结果")
    if hotspot and hotspot not in body and html.escape(hotspot) not in body:
        raise SkeletonError("报告里找不到热点原文——运行数据可能没落全")


def report_path(suffix: str, *, date: str, reports_dir: Path = REPORTS_DIR) -> Path:
    """样例报告文件名：`skeleton-<日期>.<后缀>`（对齐 S2.8 的评测报告落盘习惯）。"""
    return reports_dir / f"skeleton-{date}.{suffix}"


def worker_command() -> list[str]:
    """Celery worker 命令；`--pool=solo` 是为了 Windows 也能跑（prefork 在 Windows 不稳）。"""
    return [sys.executable, "-m", "celery", "-A", "xhs_agent.tasks.worker:app",
            "worker", "--loglevel=info", "--pool=solo"]


def api_command(port: int) -> list[str]:
    return [sys.executable, "-m", "uvicorn", "xhs_agent.api.main:app",
            "--port", str(port), "--log-level", "warning"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walking Skeleton 端到端联调")
    parser.add_argument("--case", default=DEFAULT_CASE, help=f"评测用例 id（默认 {DEFAULT_CASE}）")
    parser.add_argument("--api-url", default="", help="只驱动已起的 API（给了就不自己起进程）")
    parser.add_argument("--no-seed", action="store_true", help="不往素材库种示例素材包")
    parser.add_argument("--keep-alive", action="store_true", help="跑完不回收自己起的进程")
    parser.add_argument("--timeout", type=int, default=300, help="等待终态的上限秒数")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# 进程编排
# ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def spawn(cmd: list[str], log_path: Path) -> tuple[subprocess.Popen, Any]:
    """起子进程并把 stdout/stderr 一起写进日志文件；返回 (进程, 文件句柄)。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("wb")
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:  # pragma: no cover - Windows 是本项目的开发环境，这里只保证跨平台不留孤儿
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), stdout=handle, stderr=subprocess.STDOUT,
                            **kwargs)
    return proc, handle


def terminate_tree(proc: subprocess.Popen | None, handle: Any = None) -> None:
    """回收进程树（Windows 用 taskkill /T，POSIX 用进程组），并关掉日志句柄。"""
    if proc is not None and proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, check=False)
        else:  # pragma: no cover
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底强杀
            proc.kill()
    if handle is not None:
        handle.close()


# ---------------------------------------------------------------------------
# 编排主体
# ---------------------------------------------------------------------------


def wait_for_health(client: httpx.Client, base_url: str, *, timeout: float) -> dict[str, Any]:
    """等 `/api/health` 返回 200（它同时探活 db / redis / llm 配置）。"""
    deadline = time.monotonic() + timeout
    last = "（还没发出请求）"
    while time.monotonic() < deadline:
        try:
            response = client.get(f"{base_url}/api/health")
            if response.status_code == 200:
                return response.json()
            last = f"{response.status_code} {response.text[:200]}"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
    raise SkeletonError(
        f"API 在 {timeout:.0f}s 内没有就绪（最后状态：{last}）。"
        "本地依赖起了吗？docker compose up -d --wait")


def wait_for_worker(cfg: AppConfig, *, timeout: float) -> bool:
    """等 Celery worker 应答（control.inspect ping）；超时返回 False。"""
    from xhs_agent.tasks.celery_app import build_celery_app

    app = build_celery_app(cfg)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        replies = app.control.inspect(timeout=1.0).ping()
        if replies:
            return True
    return False


def poll_job(client: httpx.Client, base_url: str, job_id: str, *, timeout: float) -> dict[str, Any]:
    """轮询任务状态到终态；同时把"一直排队"这种情形说清楚。"""
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.get(f"{base_url}/api/jobs/{job_id}").json()
        status = last.get("status")
        if status in TERMINAL_STATUSES:
            return last
        if status == "queued" and time.monotonic() - started > 30:
            raise SkeletonError(
                "任务 30 秒还停在 queued：worker 没在消费。看它的日志（runs/_smoke/worker.log）")
        time.sleep(3.0)
    raise SkeletonError(f"任务在 {timeout:.0f}s 内没有终态（最后：{last}）")


def run_skeleton(args: argparse.Namespace) -> int:
    """按 backlog 第四节的关键路径跑一遍，逐步骤打印。"""
    cfg = load_config()
    started_at = time.monotonic()

    print("① 配置前置检查…")
    report = preflight(cfg)
    for warning in report.warnings:
        print(f"   WARN {warning}")
    if not report.ok:
        for problem in report.problems:
            print("\n".join(problem.lines()))
        raise SkeletonError(f"配置前置检查未通过（阶段 {report.stage}）")
    print(f"   通过（阶段 {report.stage}）")

    if args.no_seed:
        print("② 素材库：--no-seed，原样使用")
    else:
        copied = seed_materials(cfg.materials_dir())
        print("② 素材库：" + (f"已种入示例素材包（{copied} 个文件）" if copied
                              else "非空，原样使用"))

    api_proc = worker_proc = None
    api_log = worker_log = None
    base_url = args.api_url.rstrip("/")
    try:
        if base_url:
            print(f"③ 进程：--api-url 指定，只驱动 {base_url}（不自己起进程）")
        else:
            port = free_port()
            base_url = f"http://127.0.0.1:{port}"
            log_dir = Path(cfg.runs_dir()) / "_smoke"
            api_proc, api_log = spawn(api_command(port), log_dir / "uvicorn.log")
            worker_proc, worker_log = spawn(worker_command(), log_dir / "worker.log")
            print(f"③ 进程：uvicorn {base_url}（pid {api_proc.pid}）、"
                  f"celery worker（pid {worker_proc.pid}），日志在 {log_dir}")

        with httpx.Client(trust_env=False, timeout=60.0) as client:
            health = wait_for_health(client, base_url, timeout=90)
            print(f"④ API 就绪：{health['status']}（db/redis/llm 三项探活）")

            if worker_proc is not None and not wait_for_worker(cfg, timeout=45):
                raise SkeletonError(
                    "Celery worker 45 秒内没有应答——看 runs/_smoke/worker.log")
            if worker_proc is not None:
                print("⑤ worker 就绪（control ping 有应答）")

            hotspot = load_hotspot(args.case)
            accepted = client.post(f"{base_url}/api/analyze", json={"hotspots": [hotspot]},
                                   headers={"X-Request-ID": "walking-skeleton"})
            if accepted.status_code != 202:
                raise SkeletonError(f"POST /api/analyze 返回 {accepted.status_code}："
                                    f"{accepted.text[:300]}")
            submission = accepted.json()
            print(f"⑥ POST /api/analyze → 202 job={submission['job_id']} run={submission['run_id']}")

            job = poll_job(client, base_url, submission["job_id"], timeout=float(args.timeout))
            print(f"⑦ GET /api/jobs → {job['status']}（progress {job['progress']}）")
            if job["status"] != "succeeded":
                raise SkeletonError(f"任务失败：{json.dumps(job.get('error'), ensure_ascii=False)}")

            detail_response = client.get(f"{base_url}/api/runs/{submission['run_id']}")
            if detail_response.status_code != 200:
                raise SkeletonError(f"GET /api/runs 返回 {detail_response.status_code}")
            detail = detail_response.json()
            top = verify_run_detail(detail)
            print(f"⑧ GET /api/runs → 候选 {len(detail['hotspots'][0]['candidates'])} 条，"
                  f"首选 {top['material']['title'] or top['material']['path']}"
                  f"（命中 {len(top['hits'])} 个要素，得分 {top['score']}）")

            html_body = client.get(f"{base_url}/api/runs/{submission['run_id']}/report",
                                   params={"format": "html"}).text
            md_body = client.get(f"{base_url}/api/runs/{submission['run_id']}/report",
                                 params={"format": "md"}).text
            verify_report(html_body, fmt="html", hotspot=hotspot)
            verify_report(md_body, fmt="md", hotspot=hotspot)

            date = dt.date.today().isoformat()
            html_path = report_path("html", date=date)
            md_path = report_path("md", date=date)
            html_path.write_text(html_body, encoding="utf-8")
            md_path.write_text(md_body, encoding="utf-8")
            print(f"⑨ GET /api/runs/…/report → HTML {len(html_body)} 字符、"
                  f"Markdown {len(md_body)} 字符，已存 {html_path.name} / {md_path.name}")

        totals = detail.get("totals") or {}
        latency = time.monotonic() - started_at
        print(f"🎉 Walking Skeleton 跑通：模型调用 {totals.get('llm_calls')} 次、"
              f"成本 {totals.get('cost_cny')} 元、端到端 {latency:.1f}s")
        print(f"   运行已保留在库里：GET {base_url}/api/runs/{submission['run_id']}")
        return 0
    except (SkeletonError, ConfigError) as exc:
        message = getattr(exc, "message", None) or str(exc)
        print(f"\n❌ {message}", file=sys.stderr)
        fix = getattr(exc, "fix", "")
        if fix:
            print(f"修复：{fix}", file=sys.stderr)
        return 1
    finally:
        if not args.keep_alive:
            terminate_tree(worker_proc, worker_log)
            terminate_tree(api_proc, api_log)
        elif worker_proc is not None and api_proc is not None:
            print(f"（--keep-alive：进程保留，pid worker={worker_proc.pid} api={api_proc.pid}）")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    return run_skeleton(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
