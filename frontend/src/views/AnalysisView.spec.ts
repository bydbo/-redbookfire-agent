import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { ImageClueResult } from '@/api/analysis'
import { parseImageClue, submitAnalysis } from '@/api/analysis'
import { ApiRequestError } from '@/api/client'
import { useAnalysisStore } from '@/stores/analysis'
import AnalysisView from '@/views/AnalysisView.vue'

// 只替换网络调用，其余（ELEMENT_TYPE_LABELS 等）保留真身——视图渲染要用它们
vi.mock('@/api/analysis', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/analysis')>()
  return { ...actual, parseImageClue: vi.fn(), submitAnalysis: vi.fn(), getJob: vi.fn() }
})

const ACCEPTED = { job_id: 'job-1', run_id: 'run-1' }

function parsedResult(): ImageClueResult {
  return {
    raw_text: '球场上的球拍特写',
    clue: {
      why_it_works: ['反差感'],
      mechanisms: [{ name: '反差', explain: '预期与画面不符' }],
      elements: [{ type: 'scene', value: '球场', weight: 0.8, confidence: 0.7 }],
      borrow_angles: ['从运动场景切入'],
      risk_notes: ['不可直用肖像'],
    },
    prompt_versions: { image_hotspot_clue: 1 },
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
  await router.push('/')
  await router.isReady()
  const wrapper = mount(AnalysisView, { global: { plugins: [router] } })
  return { wrapper, router }
}

type Wrapper = Awaited<ReturnType<typeof mountView>>['wrapper']

function button(wrapper: Wrapper, label: string) {
  return wrapper.findAll('button').find((item) => item.text() === label)
}

/** 图片卡片里的描述框（分析台那个热点框也是 textarea，用 placeholder 区分）。 */
function imageTextarea(wrapper: Wrapper) {
  return wrapper.find('textarea[placeholder="模型给的热点描述，可以直接改"]')
}

async function upload(wrapper: Wrapper, name = 'a.png'): Promise<void> {
  const input = wrapper.find('input[type="file"]')
  const file = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], name, { type: 'image/png' })
  Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
  await input.trigger('change')
  await flushPromises()
}

describe('AnalysisView 图片入口（S6.8）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.mocked(parseImageClue).mockReset()
    vi.mocked(submitAnalysis).mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('有「从图片拆热点」卡片，且提供三个解析入口的文案', async () => {
    const { wrapper } = await mountView()
    expect(wrapper.text()).toContain('从图片拆热点')
    expect(wrapper.text()).toContain('选择图片')
    expect(wrapper.text()).toContain('粘贴')
    expect(wrapper.find('input[type="file"]').exists()).toBe(true)
    expect(parseImageClue).not.toHaveBeenCalled()
  })

  it('解析成功后展示可编辑描述、要素卡与三个操作按钮', async () => {
    vi.mocked(parseImageClue).mockResolvedValue(parsedResult())
    const { wrapper } = await mountView()

    await upload(wrapper)

    expect(parseImageClue).toHaveBeenCalledTimes(1)
    expect((imageTextarea(wrapper).element as HTMLTextAreaElement).value).toBe('球场上的球拍特写')
    expect(wrapper.text()).toContain('爆点要素')
    expect(wrapper.text()).toContain('球场')
    expect(wrapper.text()).toContain('从运动场景切入')
    expect(button(wrapper, '重解析')).toBeDefined()
    expect(button(wrapper, '丢弃')).toBeDefined()
    expect(button(wrapper, '用这条热点分析')).toBeDefined()
  })

  it('解析失败时把后端文案显示成错误提示', async () => {
    vi.mocked(parseImageClue).mockRejectedValue(
      new ApiRequestError('只支持 jpg / png / webp 图片（按文件内容判断）', 'bad_request', 400))
    const { wrapper } = await mountView()

    await upload(wrapper, 'a.gif')

    expect(wrapper.text()).toContain('只支持 jpg / png / webp 图片（按文件内容判断）')
    expect(imageTextarea(wrapper).exists()).toBe(false)
  })

  it('丢弃后结果区消失，重解析是空操作', async () => {
    vi.mocked(parseImageClue).mockResolvedValue(parsedResult())
    const { wrapper } = await mountView()
    await upload(wrapper)

    await button(wrapper, '丢弃')?.trigger('click')

    expect(imageTextarea(wrapper).exists()).toBe(false)
    expect(button(wrapper, '重解析')).toBeUndefined()
  })

  it('改过的描述与要素会跟着「用这条热点分析」一起提交（单条）', async () => {
    vi.mocked(parseImageClue).mockResolvedValue(parsedResult())
    vi.mocked(submitAnalysis).mockResolvedValue(ACCEPTED)
    const { wrapper } = await mountView()
    await upload(wrapper)

    await imageTextarea(wrapper).setValue('球拍特写引发的运动热')
    await wrapper.find('input[placeholder="要素取值"]').setValue('羽毛球场')
    await button(wrapper, '用这条热点分析')?.trigger('click')
    await flushPromises()

    expect(submitAnalysis).toHaveBeenCalledWith({
      hotspots: ['球拍特写引发的运动热'],
      topk: 5,
      clues: [expect.objectContaining({
        hotspot_raw: '球拍特写引发的运动热',
        elements: [expect.objectContaining({ value: '羽毛球场' })],
      })],
    })
  })

  it('「加入热点列表」把图片结果并进分析台并打上「来自图片」标签', async () => {
    vi.mocked(parseImageClue).mockResolvedValue(parsedResult())
    const { wrapper } = await mountView()
    await upload(wrapper)

    await button(wrapper, '加入热点列表')?.trigger('click')
    await flushPromises()

    const store = useAnalysisStore()
    expect(store.hotspots).toHaveLength(2)
    expect(store.hotspots[1]).toMatchObject({ text: '球场上的球拍特写' })
    expect(wrapper.text()).toContain('来自图片')
    expect(imageTextarea(wrapper).exists()).toBe(false)   // 图片卡片已清空
  })
})
