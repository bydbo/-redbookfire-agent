/* E7 界面回归：本地浏览器 + 固定 API 样本；数据库行为由集成测试单独验证。 */
const { spawn } = require('node:child_process')
const fs = require('node:fs/promises')
const path = require('node:path')
const assert = require('node:assert/strict')

const root = path.resolve(__dirname, '..')
const output = path.resolve(root, process.env.CONTEXT_SMOKE_OUTPUT || 'runs/_e7b6_final')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const port = Number(process.env.CONTEXT_SMOKE_PORT || 5188)
const origin = `http://127.0.0.1:${port}`
const time = '2026-10-18T12:00:00Z'
const sid = '11111111-1111-4111-8111-111111111111'
const sid2 = '22222222-2222-4222-8222-222222222222'
const run = '33333333-3333-4333-8333-333333333333'
const draftKey = `draft:${run}:1`
const clueKey = `clue:images:${'a'.repeat(64)}`
const ids = Object.fromEntries(['d0', 'd1', 'c0', 'c1', 'c2'].map((name, i) =>
  [name, `44444444-4444-4444-8444-${String(i + 1).padStart(12, '0')}`]))
const checks = []
let child, browser

function version(name, key, number, content) {
  return { session_id: sid, version_id: ids[name], object_key: key,
    object_type: key.startsWith('draft:') ? 'draft' : 'clue', status: 'succeeded',
    version_no: number, content, prompt_versions: {}, usage: {}, created_at: time,
    source: { message_id: 'source1' } }
}
const versions = {
  d0: version('d0', draftKey, 1, { titles: [{ text: '从这版开始', style: '建议' }], body: 'D0 历史初稿正文', tags: ['夜跑'] }),
  d1: version('d1', draftKey, 2, { titles: [{ text: '新的表达', style: '建议' }], body: 'D1 最新文案正文', tags: ['夜跑'] }),
  c0: version('c0', clueKey, 1, { raw_text: 'C0 公园夜跑', clue: { hotspot_raw: 'C0 公园夜跑',
    why_it_works: ['行动简单'], mechanisms: [{ name: '可执行', explain: '从走路开始' }],
    elements: [{ type: 'topic', value: '夜跑', weight: 0.9, confidence: 0.9, evidence: '夜跑' }],
    match_keywords: ['夜跑'], borrow_angles: ['慢慢开始'], risk_notes: [] }, edited_fields: [] }),
}
const contexts = Object.fromEntries([sid, sid2].map(id => [id, {
  session_id: id, state_version: 0, topic_id: 'topic1', recovery_status: 'complete',
  facts: [], proposals: [], object_refs: [], artifact_heads: [], effective_mode: 'enforce',
  blocking_proposals: 0, recovery: { status: 'complete' },
}]))
contexts[sid].artifact_heads = [versions.d1, versions.c0].map(v => ({
  object_key: v.object_key, version_id: v.version_id, version_no: v.version_no, updated_at: time,
}))
const source = { message_id: 'source1', session_id: sid, role: 'user', status: 'succeeded',
  content: '固定来源：不要使用感叹号。', created_at: time, attachments: [], tool_calls: [] }
const messages = { [sid]: [source], [sid2]: [] }
const titles = { [sid]: '版本与线索回归', [sid2]: '另一条对话' }
const sessions = () => ({ items: [sid, sid2].map(id => ({ session_id: id, title: titles[id],
  created_at: time, updated_at: time, message_count: messages[id].length, last_message_preview: '' })), total: 2 })
const detail = id => ({ ...sessions().items.find(s => s.session_id === id), messages: messages[id] })
const result = (id, action, extra = {}) => ({ session_id: id, state_version: contexts[id].state_version,
  status: 'succeeded', action, decision: 'applied', context: contexts[id], ...extra })
const recorded = []

async function mock(route) {
  const request = route.request(), url = new URL(request.url()), method = request.method()
  const relative = url.pathname.replace('/api', '')
  const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  if (relative === '/chat/sessions') return reply(sessions())
  const match = relative.match(/^\/chat\/sessions\/([^/]+)(.*)$/)
  if (!match) return reply({ code: 'NOT_FOUND', message: relative }, 404)
  const [, id, suffix] = match
  if (!suffix) return reply(detail(id))
  if (suffix === '/context') return reply(contexts[id])
  if (suffix === '/artifacts' && method === 'GET') {
    const key = url.searchParams.get('object_key')
    return reply({ session_id: id, items: Object.values(versions).filter(v => v.object_key === key).reverse() })
  }
  if (suffix === '/artifacts/clue-versions') {
    const body = request.postDataJSON(); recorded.push({ kind: 'edit', body })
    versions.c1 = version('c1', clueKey, 2, body.content)
    contexts[id].state_version++
    contexts[id].object_refs = [{ object_key: clueKey, object_type: 'clue', version_id: ids.c1 }]
    contexts[id].artifact_heads[1] = { ...contexts[id].artifact_heads[1], version_id: ids.c1, version_no: 2 }
    return reply(result(id, 'edit_clue', { version_id: ids.c1, version: versions.c1 }), 201)
  }
  if (suffix.endsWith('/select')) {
    const selected = suffix.split('/')[2]
    contexts[id].state_version++
    contexts[id].object_refs = [{ object_key: clueKey, object_type: 'clue', version_id: selected }]
    return reply(result(id, 'select_clue', { version_id: selected }))
  }
  if (suffix.startsWith('/artifacts/')) return reply(Object.values(versions).find(v => v.version_id === suffix.split('/')[2]))
  if (suffix === '/messages' && method === 'POST') {
    const raw = request.postData() || ''
    const binding = raw.match(/name="operation_binding"\r\n\r\n([^\r]+)/)
    const body = binding ? JSON.parse(binding[1]) : null
    recorded.push({ kind: 'binding', body })
    if (body?.force_reparse) {
      versions.c2 = version('c2', clueKey, 3, { ...versions.c0.content, raw_text: 'C2 新原始解析' })
      contexts[id].artifact_heads[1] = { ...contexts[id].artifact_heads[1], version_id: ids.c2, version_no: 3 }
    }
    return route.fulfill({ status: 200, contentType: 'text/event-stream',
      body: 'event: turn_finished\ndata: {"status":"succeeded"}\n\n' })
  }
  if (suffix === '/context/actions') {
    const body = request.postDataJSON(); recorded.push({ kind: 'recovery', body })
    contexts[id].state_version++
    const count = contexts[id].recovery.scanned_user_messages || 0
    contexts[id].recovery = { status: body.action === 'complete_recovery' ? 'complete'
      : count === 0 ? 'pending' : 'awaiting_confirmation', recovery_id: 'recovery-fixed',
      scanned_user_messages: count === 0 ? 20 : 21, total_user_messages: 21, pending_proposal_ids: [], gaps: [] }
    contexts[id].recovery_status = contexts[id].recovery.status === 'complete' ? 'complete' : 'partial'
    return reply(result(id, body.action))
  }
  return reply({ code: 'NOT_FOUND', message: suffix }, 404)
}

async function main() {
  await fs.mkdir(output, { recursive: true })
  child = spawn(process.execPath, [path.join(root, 'frontend/node_modules/vite/bin/vite.js'),
    '--host', '127.0.0.1', '--port', String(port), '--strictPort'], {
    cwd: path.join(root, 'frontend'), windowsHide: true, stdio: 'ignore',
  })
  for (let tries = 0; tries < 100; tries++) {
    try { if ((await fetch(origin)).ok) break } catch { /* wait for local Vite */ }
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  browser = await chromium.launch({ headless: true, executablePath: process.env.BROWSER_EXECUTABLE })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  for (const name of ['night-run', 'camping']) {
    const svg = await fs.readFile(path.join(root, `evals/context-repair-v2/fixtures/${name}.svg`), 'utf8')
    await page.setViewportSize({ width: 640, height: 360 })
    await page.setContent(`<style>body{margin:0}</style>${svg}`)
    await page.screenshot({ path: path.join(output, `${name}.png`) })
  }
  await page.setViewportSize({ width: 1440, height: 1100 })
  await page.route(`${origin}/api/**`, mock)
  await page.goto(origin)
  await page.getByText('当前目标与限制', { exact: true }).click()
  const history = page.getByTestId('artifact-panel')
  await history.getByRole('button', { name: /文案.*查看历史版本/ }).click()
  await history.getByRole('button', { name: '第 1 版', exact: true }).click()
  await page.getByText('D0 历史初稿正文', { exact: true }).waitFor()
  await page.getByText('从这版开始', { exact: true }).waitFor()
  await history.getByRole('button', { name: '以这版为基础改稿' }).click()
  await page.getByPlaceholder('想蹭哪条热点？说一句话就行（也可以只丢图，最多 9 张）').fill('请改得更简短')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.waitForFunction(() => !document.querySelector('button[disabled]')?.textContent?.includes('发送'))
  assert.equal(recorded.find(x => x.kind === 'binding')?.body?.base_version_id, ids.d0)
  checks.push('head 为 v2 时，从历史 v1 发送的可信绑定仍为 v1')
  await history.getByRole('button', { name: /图片线索.*查看历史版本/ }).click()
  await history.getByRole('button', { name: '第 1 版', exact: true }).click()
  await history.getByRole('button', { name: '编辑这版线索' }).click()
  await page.getByPlaceholder('热点描述', { exact: true }).fill('C1 我编辑的夜跑线索')
  await history.getByRole('button', { name: '保存并选定新版' }).click()
  await page.getByText('C1 我编辑的夜跑线索', { exact: true }).waitFor()
  await page.getByText('当前选定线索', { exact: true }).waitFor()
  checks.push('结构化编辑保存新版本，并选定编辑版 C1')
  await page.screenshot({ path: path.join(output, 'ui-clue-edited.png'), fullPage: true })
  await history.getByRole('button', { name: '重新解析原图' }).click()
  await page.waitForResponse(response => response.url().endsWith('/context'))
  assert.equal(contexts[sid].object_refs[0].version_id, ids.c1)
  assert.equal(recorded.filter(x => x.kind === 'binding').at(-1).body.force_reparse, true)
  checks.push('重解析 C2 后仍选定 C1，明确重解析绑定已发送')
  await history.getByRole('button', { name: /图片线索.*查看历史版本/ }).click()
  await history.getByRole('button', { name: '第 3 版', exact: true }).click()
  await history.getByRole('button', { name: '选用这版线索' }).click()
  await page.getByText('当前选定线索', { exact: true }).waitFor()
  assert.equal(contexts[sid].object_refs[0].version_id, ids.c2)
  await history.getByRole('button', { name: '查看来源消息' }).click()
  await page.getByRole('dialog').getByText(source.content, { exact: true }).waitFor()
  await page.keyboard.press('Escape')
  checks.push('主动切换 C2 与查看来源消息可用')
  contexts[sid].recovery_status = 'partial'
  contexts[sid].recovery = { status: 'pending', scanned_user_messages: 0, total_user_messages: 21 }
  await page.reload()
  await page.getByText('当前目标与限制', { exact: true }).click()
  await page.getByRole('button', { name: '开始恢复', exact: true }).click()
  await page.getByRole('button', { name: '扫描下一页', exact: true }).click()
  await page.getByRole('button', { name: '已核对并完成', exact: true }).click()
  await page.getByText('历史状态未完整恢复：更早的要求可能没读回来，请补充关键限制', { exact: true }).waitFor({ state: 'hidden' })
  const actions = recorded.filter(x => x.kind === 'recovery').map(x => x.body)
  assert.equal(actions[1].recovery_id, 'recovery-fixed')
  assert.equal(actions[2].acknowledge_partial_history, false)
  checks.push('21 条历史两页扫描，身份连续，完成未自动确认历史缺口')
  await page.getByRole('button', { name: /另一条对话/ }).click()
  await history.waitFor({ state: 'hidden' })
  await page.screenshot({ path: path.join(output, 'ui-other-session.png'), fullPage: true })
  checks.push('切会话清空旧版本与选择')
  assert.deepEqual(errors, [])
  await fs.writeFile(path.join(output, 'browser.json'), JSON.stringify({ status: 'passed', kind: 'browser_mock_api',
    backend: 'covered separately by real PostgreSQL integration tests', checks, recorded, page_errors: errors }, null, 2))
  console.log(JSON.stringify({ status: 'passed', checks: checks.length, output }))
}
main().catch(async error => {
  if (browser) { const pages = browser.contexts()[0]?.pages(); if (pages?.length) await pages[0].screenshot({ path: path.join(output, 'ui-failed.png'), fullPage: true }) }
  console.error(error); process.exitCode = 1
}).finally(async () => { if (browser) await browser.close(); if (child) child.kill() })
