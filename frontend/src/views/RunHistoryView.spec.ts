import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { RunSummary } from '@/api/analysis'
import { listRuns } from '@/api/analysis'
import RunHistoryView from '@/views/RunHistoryView.vue'

vi.mock('@/api/analysis', () => ({ listRuns: vi.fn() }))

const PAGE_SIZE = 20

function summary(id: string, status: RunSummary['status'], preview = `热点 ${id}`): RunSummary {
  return {
    run_id: `${id}-1111-2222-3333-444444444444`,
    status,
    created_at: '2026-10-05T04:00:00+00:00',
    finished_at: status === 'succeeded' ? '2026-10-05T04:01:00+00:00' : null,
    hotspot_count: 2,
    hotspot_preview: preview,
    topk: 5,
    totals: {
      llm_calls: 6,
      prompt_tokens: 100,
      completion_tokens: 50,
      cost_cny: 0.2202,
      latency_ms: 102869,
    },
    error: status === 'failed' ? '模型调用失败' : null,
  }
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'analysis', component: { template: '<div />' } },
      { path: '/runs', name: 'run-history', component: { template: '<div />' } },
      { path: '/runs/:runId', name: 'run-result', component: { template: '<div />' } },
    ],
  })
}

async function mountView() {
  const router = makeRouter()
  await router.push('/runs')
  await router.isReady()
  const wrapper = mount(RunHistoryView, { global: { plugins: [router] } })
  await flushPromises()
  return { wrapper, router }
}

function rowTexts(wrapper: Awaited<ReturnType<typeof mountView>>['wrapper']): string[] {
  return wrapper.findAll('button').map((button) => button.text())
}

describe('RunHistoryView', () => {
  beforeEach(() => {
    vi.mocked(listRuns).mockReset()
  })

  it('按后端给的顺序渲染行，并展示预览与统计', async () => {
    vi.mocked(listRuns).mockResolvedValue({
      items: [summary('aaaa', 'succeeded', '深夜 citywalk'), summary('bbbb', 'failed')],
      total: 2,
    })
    const { wrapper } = await mountView()

    const text = wrapper.text()
    expect(text).toContain('共 2 条')
    expect(text).toContain('深夜 citywalk')
    expect(text).toContain('成本 ¥0.2202')
    expect(text).toContain('耗时 102.9s')
    // 短 id 用来区分同一页里的多行
    expect(text).toContain('#aaaa')
    expect(text).toContain('#bbbb')
    expect(listRuns).toHaveBeenCalledWith({ limit: PAGE_SIZE, offset: 0 })
  })

  it('succeeded / failed 可回看，queued / running 置灰（详情接口会 409）', async () => {
    vi.mocked(listRuns).mockResolvedValue({
      items: [summary('aaaa', 'succeeded'), summary('bbbb', 'failed'),
              summary('cccc', 'queued'), summary('dddd', 'running')],
      total: 4,
    })
    const { wrapper } = await mountView()

    const buttons = rowTexts(wrapper)
    expect(buttons.filter((label) => label === '查看结果')).toHaveLength(4)
    const disabled = wrapper.findAll('button').filter((button) => button.text() === '查看结果' && button.attributes('disabled') !== undefined)
    expect(disabled).toHaveLength(2)                       // queued 与 running
    // 未完成的行不提供报告入口（报告接口同样 409）
    expect(wrapper.findAll('a').filter((link) => (link.attributes('href') ?? '').includes('report'))).toHaveLength(4)
  })

  it('报告链接指向真实接口并带下载文件名', async () => {
    vi.mocked(listRuns).mockResolvedValue({ items: [summary('aaaa', 'succeeded')], total: 1 })
    const { wrapper } = await mountView()

    const links = wrapper.findAll('a').map((link) => ({
      href: link.attributes('href'),
      download: link.attributes('download'),
    }))
    expect(links).toEqual([
      { href: '/api/runs/aaaa-1111-2222-3333-444444444444/report?format=html',
        download: 'report-aaaa-1111-2222-3333-444444444444.html' },
      { href: '/api/runs/aaaa-1111-2222-3333-444444444444/report?format=md',
        download: 'report-aaaa-1111-2222-3333-444444444444.md' },
    ])
  })

  it('没有任何运行时给出空态', async () => {
    vi.mocked(listRuns).mockResolvedValue({ items: [], total: 0 })
    const { wrapper } = await mountView()
    expect(wrapper.text()).toContain('还没有运行记录')
    expect(wrapper.find('.n-pagination').exists()).toBe(false)
  })

  it('总数超过一页才渲染分页，翻页按 offset 取下一页', async () => {
    vi.mocked(listRuns).mockImplementation(async ({ offset }) => ({
      items: Array.from({ length: offset === 0 ? PAGE_SIZE : 5 },
                        (_, i) => summary(`p${offset + i}`, 'succeeded')),
      total: 25,
    }))
    const { wrapper } = await mountView()

    expect(wrapper.find('.n-pagination').exists()).toBe(true)
    const pageTwo = wrapper.findAll('.n-pagination-item')
      .find((item) => item.text() === '2')
    expect(pageTwo).toBeDefined()

    await pageTwo?.trigger('click')
    await flushPromises()

    expect(listRuns).toHaveBeenLastCalledWith({ limit: PAGE_SIZE, offset: PAGE_SIZE })
    expect(wrapper.text()).toContain('#p20')             // 第二页第一行的短 id
  })
})
