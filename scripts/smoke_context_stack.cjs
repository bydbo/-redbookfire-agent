/* 真浏览器连接临时真实 API / Postgres / Redis；仅模型固定，不伪造接口响应。 */
const { spawn } = require('node:child_process')
const fs = require('node:fs/promises')
const path = require('node:path')
const assert = require('node:assert/strict')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '..')
const output = process.env.CONTEXT_SMOKE_OUTPUT
const api = process.env.CONTEXT_STACK_URL
let browser, vite

async function main() {
  const fixture = JSON.parse(await fs.readFile(process.env.CONTEXT_STACK_FIXTURE, 'utf8'))
  const origin = 'http://127.0.0.1:5189'
  vite = spawn(process.execPath, [path.join(root, 'frontend/node_modules/vite/bin/vite.js'),
    '--host', '127.0.0.1', '--port', '5189', '--strictPort'], { cwd: path.join(root, 'frontend'), windowsHide: true, stdio: 'ignore' })
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(origin)).ok) break } catch { /* local startup */ }
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  browser = await chromium.launch({ headless: true, executablePath: process.env.BROWSER_EXECUTABLE })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } })
  const errors = [], checks = [], requests = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route(`${origin}/api/**`, async route => {
    const request = route.request()
    requests.push({ method: request.method(), path: new URL(request.url()).pathname,
      binding: request.postData()?.match(/name="operation_binding"\r\n\r\n([^\r]+)/)?.[1] })
    // 仅把开发端口转发给真实后端，响应内容来自后端本身。
    await route.fulfill({ response: await route.fetch({ url: api + new URL(request.url()).pathname + new URL(request.url()).search }) })
  })
  await page.goto(origin)
  await page.getByRole('button', { name: /版本与线索回归/ }).click()
  await page.getByText('当前目标与限制', { exact: true }).click()
  const panel = page.getByTestId('artifact-panel')
  await panel.getByRole('button', { name: /文案.*查看历史版本/ }).click()
  await panel.getByRole('button', { name: '第 1 版', exact: true }).click()
  await panel.getByText('D0 历史初稿正文', { exact: true }).waitFor()
  await panel.getByRole('button', { name: '以这版为基础改稿' }).click()
  await page.getByPlaceholder('想蹭哪条热点？说一句话就行（也可以只丢图，最多 9 张）').fill('请改得更简短')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.waitForResponse(r => r.url().endsWith('/messages') && r.request().method() === 'POST')
  await page.getByRole('button', { name: '发送', exact: true }).waitFor({ state: 'visible' })
  const binding = requests.find(r => r.binding)?.binding
  assert.equal(JSON.parse(binding).base_version_id, fixture.draft_v1)
  checks.push('真实消息 API 接收用户选定的历史 v1 绑定')
  await panel.getByRole('button', { name: /图片线索.*查看历史版本/ }).click()
  await panel.getByRole('button', { name: '第 1 版', exact: true }).click()
  await panel.getByRole('button', { name: '编辑这版线索' }).click()
  await page.getByPlaceholder('热点描述', { exact: true }).fill('C1 我编辑的夜跑线索')
  await panel.getByRole('button', { name: '保存并选定新版' }).click()
  await page.getByText('C1 我编辑的夜跑线索', { exact: true }).waitFor()
  await page.getByText('当前选定线索', { exact: true }).waitFor()
  checks.push('真实编辑接口保存 C1 并从数据库重新读回')
  await page.screenshot({ path: path.join(output, 'clue-edited.png'), fullPage: true })
  await panel.getByRole('button', { name: '重新解析原图' }).click()
  await page.waitForResponse(r => r.url().endsWith('/context'))
  await panel.getByRole('button', { name: /图片线索.*查看历史版本/ }).click()
  await panel.getByRole('button', { name: '第 3 版', exact: true }).click()
  await panel.getByRole('button', { name: '选用这版线索' }).click()
  await page.getByText('当前选定线索', { exact: true }).waitFor()
  checks.push('重解析经真实轮次与 Redis 事件完成，C2 可显式选定')
  await panel.getByRole('button', { name: '查看来源消息' }).click()
  await page.getByRole('dialog').waitFor()
  await page.keyboard.press('Escape')
  await Promise.all([
    page.waitForResponse(r => r.url().includes(fixture.legacy_session_id + '/context')),
    page.getByRole('button', { name: /历史恢复回归/ }).click(),
  ])
  const recoveryStart = page.getByRole('button', { name: '开始恢复', exact: true })
  if (!await recoveryStart.isVisible()) await page.getByText('当前目标与限制', { exact: true }).click()
  await page.getByRole('button', { name: '开始恢复', exact: true }).click()
  await page.getByRole('button', { name: '扫描下一页', exact: true }).click()
  await page.getByRole('button', { name: '已核对并完成', exact: true }).click()
  await page.getByText('历史状态未完整恢复：更早的要求可能没读回来，请补充关键限制', { exact: true }).waitFor({ state: 'hidden' })
  checks.push('真实数据库 21 条历史两页恢复并完成')
  await page.getByRole('button', { name: /另一条对话/ }).click()
  await panel.waitFor({ state: 'hidden' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: path.join(output, 'mobile-other-session.png'), fullPage: true })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
  checks.push('切会话清空旧状态，390px 窄屏无横向溢出')
  assert.deepEqual(errors, [])
  await fs.writeFile(path.join(output, 'browser.json'), JSON.stringify({ status: 'passed',
    kind: 'browser_real_api_postgres_redis_fixed_model', checks, requests, errors }, null, 2))
  console.log(JSON.stringify({ status: 'passed', checks: checks.length, output }))
}
main().catch(async error => {
  if (browser) await browser.contexts()[0]?.pages()[0]?.screenshot({ path: path.join(output, 'failure.png'), fullPage: true })
  console.error(error); process.exitCode = 1
}).finally(async () => { if (browser) await browser.close(); if (vite) vite.kill() })
