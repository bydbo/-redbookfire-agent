"""E9 单任务浏览器尝试：真实 UI 新建会话 → 提交 → 等待结果 → 刷新核验。

前置：API / Redis / worker / 模型转发已由调用方启动；本脚本不启动堆栈、不预种 run。
输入：`--base-url`、`--case-id`、`--text`、可选有序 `--image`、`--budget-ref`。
输出：`attempt.json`（E2EAttempt）并追加到 `--result-file`（默认 results/e2e.json）。

真实效果必须用 `--mode live`；`engineering_fake` 只用于工程链路诊断，永远不计入真实汇总。
人工语义评分不由本脚本代填；未评分时 evidence 检查会保持未完成。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

from xhs_agent.evaluation.e2e import E2EAttempt, E2EResult, EvidenceRefs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BROWSER = r"""
const { chromium } = require(process.env.PLAYWRIGHT_MODULE)
const fs = require('node:fs/promises')
const path = require('node:path')

;(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.E9_EDGE, headless: true, args: ['--no-proxy-server'],
  })
  const page = await browser.newPage({ viewport: { width: 1360, height: 1000 } })
  const out = process.env.E9_OUTPUT
  await fs.mkdir(out, { recursive: true })
  const result = { ok: false, error: '', session_id: '', message_id: '', run_id: '',
                   ui_state: '', checks: {}, report_consistent: false, refreshed_ok: false }
  let sessionId = ''
  page.on('response', async (response) => {
    if (response.url().endsWith('/api/chat/sessions') && response.request().method() === 'POST') {
      try { sessionId = (await response.json()).session_id || '' } catch (_) {}
    }
  })
  try {
    await page.goto(process.env.E9_BASE_URL, { waitUntil: 'networkidle' })
    const newButton = page.getByRole('button', { name: '新对话', exact: true })
    if (await newButton.count()) await newButton.click()
    await page.waitForResponse(r => r.url().endsWith('/api/chat/sessions')
      && r.request().method() === 'POST', { timeout: 15000 }).catch(() => null)
    await page.getByPlaceholder('想蹭哪条热点？说一句话就行（也可以只丢图，最多 9 张）')
      .fill(process.env.E9_TEXT)
    const files = (process.env.E9_IMAGES || '').split(path.delimiter).filter(Boolean)
    if (files.length) await page.locator('input[type=file]').first().setInputFiles(files)
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await page.waitForResponse(r => r.url().includes('/messages')
      && r.request().method() === 'POST', { timeout: 15000 }).catch(() => null)
    await page.waitForFunction(() => /候选素材（|覆盖缺口|明确缺口|没有命中的素材/.test(
      document.body.innerText), { timeout: 180000 })
    await page.waitForFunction(() => {
      const button = [...document.querySelectorAll('button')]
        .find(item => item.textContent.trim() === '发送')
      return Boolean(button && !button.disabled)
    }, { timeout: 180000 })
    await page.screenshot({ path: path.join(out, 'result.png'), fullPage: true })
    result.ui_state = 'result'
    result.checks.ui_result = true

    if (!sessionId) {
      const listing = await page.evaluate(async () =>
        (await fetch('/api/chat/sessions?limit=5&offset=0')).json())
      sessionId = listing.items?.[0]?.session_id || ''
    }
    result.session_id = sessionId
    const detail = await page.evaluate(async (id) => {
      const response = await fetch(`/api/chat/sessions/${id}?limit=200`)
      return response.ok ? await response.json() : null
    }, sessionId)
    const assistant = (detail?.messages || []).filter(m => m.role === 'assistant').at(-1)
    result.message_id = assistant?.message_id || ''
    result.run_id = assistant?.run_id || ''
    result.checks.message_persisted = Boolean(result.message_id)
    result.checks.run_persisted = Boolean(result.run_id)

    let reportMd = ''
    let run = null
    if (result.run_id) {
      run = await page.evaluate(async (id) => {
        const response = await fetch(`/api/runs/${id}`)
        return response.ok ? await response.json() : null
      }, result.run_id)
      reportMd = await page.evaluate(async (id) => {
        const response = await fetch(`/api/runs/${id}/report?format=md`)
        return response.ok ? await response.text() : ''
      }, result.run_id)
      const hotspot = run?.hotspots?.[0]
      const title = hotspot?.candidates?.[0]?.material?.title || ''
      result.report_consistent = Boolean(reportMd) && (
        title ? reportMd.includes(title) : /缺口|没有命中/.test(reportMd))
      await fs.writeFile(path.join(out, 'run.json'), JSON.stringify(run, null, 2))
      await fs.writeFile(path.join(out, 'report.md'), reportMd, 'utf8')
    }
    result.checks.report_consistent = result.report_consistent

    await page.reload({ waitUntil: 'networkidle' })
    await page.waitForFunction(() => /候选素材（|覆盖缺口|明确缺口|没有命中的素材/.test(
      document.body.innerText), { timeout: 60000 })
    const latest = await page.evaluate(async () =>
      (await fetch('/api/chat/sessions?limit=5&offset=0')).json())
    result.refreshed_ok = latest.items?.[0]?.session_id === sessionId
    result.checks.refresh_restored = result.refreshed_ok
    await page.screenshot({ path: path.join(out, 'refreshed.png'), fullPage: true })
    result.ok = Object.values(result.checks).every(Boolean)
  } catch (err) {
    result.error = String(err && err.stack || err)
    await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {})
  } finally {
    await browser.close()
    await fs.writeFile(path.join(out, 'browser-result.json'),
      JSON.stringify(result, null, 2), 'utf8')
    console.log(JSON.stringify(result))
    process.exit(result.ok ? 0 : 1)
  }
})()
"""


def _append_result(path: Path, attempt: E2EAttempt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        result = E2EResult.model_validate(json.loads(path.read_text(encoding="utf-8")))
        rows = [item for item in result.attempts if item.attempt_id != attempt.attempt_id]
    else:
        rows = []
    rows.append(attempt)
    path.write_text(json.dumps(
        {"attempts": [item.model_dump() for item in rows]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E9 单任务浏览器尝试")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--repeat", type=int, default=1, choices=(1, 2, 3))
    parser.add_argument("--text", required=True)
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--mode", choices=("live", "engineering_fake"), default="engineering_fake")
    parser.add_argument("--budget-ref", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--result-file", default="")
    parser.add_argument("--playwright-module", default=os.environ.get("PLAYWRIGHT_MODULE", ""))
    parser.add_argument("--edge", default=os.environ.get(
        "SMOKE_EDGE", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"))
    args = parser.parse_args(argv)

    if args.mode == "live" and not args.budget_ref:
        print("live 模式必须提供 --budget-ref，不能绕过费用账本", file=sys.stderr)
        return 3
    if not Path(args.playwright_module).is_file() or not Path(args.edge).is_file():
        print("缺少 PLAYWRIGHT_MODULE 或 Edge", file=sys.stderr)
        return 3
    if len(args.image) > 9:
        print("图片最多 9 张", file=sys.stderr)
        return 3
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else (
        PROJECT_ROOT / "runs" / "_e9" / "browser" / f"{stamp}-{args.case_id}")
    out.mkdir(parents=True, exist_ok=True)
    result_file = Path(args.result_file) if args.result_file else (
        PROJECT_ROOT / "evals" / "e9-v1" / "results" / "e2e.json")
    browser_file = out / "browser.cjs"
    browser_file.write_text(BROWSER, encoding="utf-8")
    env = {
        **os.environ,
        "PLAYWRIGHT_MODULE": args.playwright_module,
        "E9_EDGE": args.edge,
        "E9_BASE_URL": args.base_url,
        "E9_TEXT": args.text,
        "E9_IMAGES": os.pathsep.join(str(Path(item).resolve()) for item in args.image),
        "E9_OUTPUT": str(out),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    node = subprocess.run(["node", str(browser_file)], env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300)
    (out / "browser.log").write_text((node.stdout or "") + "\n" + (node.stderr or ""),
                                     encoding="utf-8")
    payload = {}
    try:
        payload = json.loads(node.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        payload = {"error": (node.stderr or "浏览器没有返回 JSON")[:1000]}
    attempt = E2EAttempt(
        attempt_id=f"{args.case_id}:browser:r{args.repeat}",
        case_id=args.case_id,
        repeat=args.repeat,
        mode=args.mode,
        status="completed" if node.returncode == 0 else "failed",
        input_summary=args.text[:200],
        refs=EvidenceRefs(
            session_id=str(payload.get("session_id") or ""),
            message_id=str(payload.get("message_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            budget_ref=args.budget_ref,
        ),
        ui_end_state=str(payload.get("ui_state") or ""),
        refreshed_ok=bool(payload.get("refreshed_ok")),
        report_consistent=bool(payload.get("report_consistent")),
        auto_checks={str(key): bool(value)
                     for key, value in dict(payload.get("checks") or {}).items()},
        error=str(payload.get("error") or ""),
    )
    (out / "attempt.json").write_text(attempt.model_dump_json(indent=2) + "\n", encoding="utf-8")
    _append_result(result_file, attempt)
    print(json.dumps({"attempt_id": attempt.attempt_id, "status": attempt.status,
                      "out": str(out), "result_file": str(result_file)},
                     ensure_ascii=False, indent=2))
    return 0 if attempt.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
