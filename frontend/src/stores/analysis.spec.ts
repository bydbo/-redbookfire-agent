import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { JobStatus } from '@/api/analysis'
import type { HotspotClue, ImageClueResult } from '@/api/analysis'
import { getJob, parseImageClue, submitAnalysis } from '@/api/analysis'
import { ApiRequestError } from '@/api/client'
import { useAnalysisStore } from '@/stores/analysis'

vi.mock('@/api/analysis', () => ({
  submitAnalysis: vi.fn(),
  getJob: vi.fn(),
  parseImageClue: vi.fn(),
}))

const POLL_MS = 2000
const ACCEPTED = { job_id: 'job-1', run_id: 'run-1' }

function job(status: JobStatus['status'], extra: Partial<JobStatus> = {}): JobStatus {
  return { job_id: 'job-1', run_id: 'run-1', status, progress: 0, ...extra }
}

/** 每次调用都给一份新对象：store 的要素编辑是就地改的，用例之间不能共享同一份。 */
function parsedClue(): HotspotClue {
  return {
    why_it_works: ['反差感'],
    mechanisms: [{ name: '反差', explain: '预期与画面不符' }],
    elements: [{ type: 'scene', value: '球场', weight: 0.8, confidence: 0.7 }],
    match_keywords: ['球拍'],
    borrow_angles: ['从运动场景切入'],
    risk_notes: ['不可直用肖像'],
  }
}

function parsedResult(): ImageClueResult {
  return { raw_text: '球场上的球拍特写', clue: parsedClue(), prompt_versions: { image_hotspot_clue: 1 } }
}

describe('useAnalysisStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.mocked(submitAnalysis).mockReset()
    vi.mocked(getJob).mockReset()
    vi.mocked(parseImageClue).mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  describe('提交前校验（契约 AnalyzeRequest 的口径）', () => {
    it('空输入被拦下', async () => {
      const store = useAnalysisStore()
      store.hotspots = [{ text: '   ' }]
      await expect(store.submit()).resolves.toBe('请至少填写一个热点')
    })

    it('超过 10 条被拦下', async () => {
      const store = useAnalysisStore()
      store.hotspots = Array.from({ length: 11 }, (_, i) => ({ text: `热点${i}` }))
      await expect(store.submit()).resolves.toBe('热点最多 10 个')
    })

    it('单条超过 500 字被拦下', async () => {
      const store = useAnalysisStore()
      store.hotspots = [{ text: '长'.repeat(501) }]
      await expect(store.submit()).resolves.toBe('单条热点不能超过 500 字')
    })

    it('topk 越界被拦下', async () => {
      const store = useAnalysisStore()
      store.hotspots = [{ text: '热点' }]
      store.topk = 21
      await expect(store.submit()).resolves.toBe('候选数 topk 需在 1–20 之间')
    })
  })

  it('提交成功后进入轮询，空白项被剔除后再发出去', async () => {
    vi.mocked(submitAnalysis).mockResolvedValue(ACCEPTED)
    const store = useAnalysisStore()
    store.hotspots = [{ text: '  热点A  ' }, { text: '   ' }]
    store.topk = 3

    await expect(store.submit()).resolves.toBeNull()

    expect(submitAnalysis).toHaveBeenCalledWith({ hotspots: ['热点A'], topk: 3 })
    expect(store.phase).toBe('polling')
    expect(store.job).toEqual({ ...ACCEPTED, status: 'queued', progress: 0 })
    store.reset()
  })

  it('轮询到 succeeded 后停止轮询并落到终态', async () => {
    vi.mocked(submitAnalysis).mockResolvedValue(ACCEPTED)
    vi.mocked(getJob)
      .mockResolvedValueOnce(job('running', { progress: 0.5 }))
      .mockResolvedValueOnce(job('succeeded', { progress: 1 }))

    const store = useAnalysisStore()
    store.hotspots = [{ text: '热点' }]
    await store.submit()

    await vi.advanceTimersByTimeAsync(POLL_MS)
    expect(store.job?.status).toBe('running')
    await vi.advanceTimersByTimeAsync(POLL_MS)

    expect(store.phase).toBe('succeeded')
    const callsAfterDone = vi.mocked(getJob).mock.calls.length
    await vi.advanceTimersByTimeAsync(POLL_MS * 3)
    expect(vi.mocked(getJob).mock.calls.length).toBe(callsAfterDone)   // 已停轮询
  })

  it('轮询到 failed 时带上后端给的失败详情', async () => {
    vi.mocked(submitAnalysis).mockResolvedValue(ACCEPTED)
    vi.mocked(getJob).mockResolvedValue(job('failed', {
      error: { code: 'internal_error', message: '模型调用失败', detail: { step: 'copy_draft' } },
    }))

    const store = useAnalysisStore()
    store.hotspots = [{ text: '热点' }]
    await store.submit()
    await vi.advanceTimersByTimeAsync(POLL_MS)

    expect(store.phase).toBe('failed')
    expect(store.failure?.message).toBe('模型调用失败')
    expect(store.failure?.detail).toContain('copy_draft')
  })

  it('轮询请求本身出错也算失败', async () => {
    vi.mocked(submitAnalysis).mockResolvedValue(ACCEPTED)
    vi.mocked(getJob).mockRejectedValue(new ApiRequestError('任务不存在', 'not_found', 404))

    const store = useAnalysisStore()
    store.hotspots = [{ text: '热点' }]
    await store.submit()
    await vi.advanceTimersByTimeAsync(POLL_MS)

    expect(store.phase).toBe('failed')
    expect(store.failure?.message).toBe('任务不存在')
  })

  it('提交本身失败时直接落到 failed', async () => {
    vi.mocked(submitAnalysis).mockRejectedValue(new ApiRequestError('队列不可用', 'dependency_unavailable', 503))
    const store = useAnalysisStore()
    store.hotspots = [{ text: '热点' }]

    await expect(store.submit()).resolves.toBeNull()
    expect(store.phase).toBe('failed')
    expect(store.failure?.message).toBe('队列不可用')
  })
})

describe('图片热点解析（S6.8）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.mocked(submitAnalysis).mockReset()
    vi.mocked(getJob).mockReset()
    vi.mocked(parseImageClue).mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  function pngFile(name = 'a.png'): File {
    return new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], name, { type: 'image/png' })
  }

  it('解析成功后存下文件、描述与可编辑要素', async () => {
    vi.mocked(parseImageClue).mockResolvedValue(parsedResult())
    const store = useAnalysisStore()
    const file = pngFile()

    await store.parseImage(file)

    expect(parseImageClue).toHaveBeenCalledWith(file)
    expect(store.imagePhase).toBe('parsed')
    expect(store.imageFile).toBe(file)
    expect(store.imageDraftText).toBe('球场上的球拍特写')
    expect(store.imageElements).toHaveLength(1)
    expect(store.imageError).toBeNull()
  })

  it('解析失败落到 failed 并给出后端文案；重解析复用内存里那份 File', async () => {
    vi.mocked(parseImageClue).mockRejectedValueOnce(
      new ApiRequestError('只支持 jpg / png / webp 图片（按文件内容判断）', 'bad_request', 400))
    const store = useAnalysisStore()
    const file = pngFile('a.gif')

    await store.parseImage(file)
    expect(store.imagePhase).toBe('failed')
    expect(store.imageError).toContain('jpg / png / webp')
    expect(store.imageResult).toBeNull()

    vi.mocked(parseImageClue).mockResolvedValueOnce(parsedResult())
    await store.reparse()

    expect(parseImageClue).toHaveBeenLastCalledWith(file)
    expect(store.imagePhase).toBe('parsed')
    expect(store.imageError).toBeNull()
  })

  it('丢弃后回到 idle；此时重解析是空操作（不再打接口）', async () => {
    vi.mocked(parseImageClue).mockResolvedValue(parsedResult())
    const store = useAnalysisStore()
    await store.parseImage(pngFile())

    store.discardImage()

    expect(store.imagePhase).toBe('idle')
    expect(store.imageFile).toBeNull()
    expect(store.imageResult).toBeNull()
    expect(store.imageDraftText).toBe('')

    vi.mocked(parseImageClue).mockClear()
    await store.reparse()
    expect(parseImageClue).not.toHaveBeenCalled()
  })

  it('要素卡可改、可增、可删', async () => {
    vi.mocked(parseImageClue).mockResolvedValue(parsedResult())
    const store = useAnalysisStore()
    await store.parseImage(pngFile())

    store.updateElement(0, { value: '羽毛球场', weight: 0.9 })
    expect(store.imageElements[0]).toMatchObject({ value: '羽毛球场', weight: 0.9 })

    store.addElement()
    expect(store.imageElements).toHaveLength(2)
    expect(store.imageElements[1]).toMatchObject({ type: 'topic', weight: 0.6 })

    store.removeElement(0)
    expect(store.imageElements).toHaveLength(1)
    expect(store.imageElements[0].value).toBe('')
  })

  it('带 clue 提交时组装等长 clues：无 clue 补 null，hotspot_raw 同步成当前文本', async () => {
    vi.mocked(submitAnalysis).mockResolvedValue(ACCEPTED)
    const store = useAnalysisStore()
    store.hotspots = [{ text: '手动热点' }, { text: '  图片热点  ', clue: parsedClue() }]

    await store.submit()

    const payload = vi.mocked(submitAnalysis).mock.calls[0][0]
    expect(payload.hotspots).toEqual(['手动热点', '图片热点'])
    expect(payload.clues).toHaveLength(2)
    expect(payload.clues?.[0]).toBeNull()
    expect(payload.clues?.[1]).toMatchObject({
      hotspot_raw: '图片热点',
      elements: [{ type: 'scene', value: '球场', weight: 0.8, confidence: 0.7 }],
    })
    store.reset()
  })

  it('空白项被剔除后，clues 与过滤后的 hotspots 仍然一一对应', async () => {
    vi.mocked(submitAnalysis).mockResolvedValue(ACCEPTED)
    const store = useAnalysisStore()
    store.hotspots = [{ text: '  ' }, { text: '图片热点', clue: parsedClue() }]

    await store.submit()

    const payload = vi.mocked(submitAnalysis).mock.calls[0][0]
    expect(payload.hotspots).toEqual(['图片热点'])
    expect(payload.clues).toEqual([expect.objectContaining({ hotspot_raw: '图片热点' })])
    store.reset()
  })

  it('一条 clue 都没有时不发 clues 字段（与 S6.7 之前的请求体一致）', async () => {
    vi.mocked(submitAnalysis).mockResolvedValue(ACCEPTED)
    const store = useAnalysisStore()
    store.hotspots = [{ text: '热点A' }]
    store.topk = 3

    await store.submit()

    const payload = vi.mocked(submitAnalysis).mock.calls[0][0]
    expect(payload).toEqual({ hotspots: ['热点A'], topk: 3 })
    expect('clues' in payload).toBe(false)
    store.reset()
  })

  it('「加入热点列表」把解析结果并进列表（带 clue），并清掉图片卡片', async () => {
    vi.mocked(parseImageClue).mockResolvedValue(parsedResult())
    const store = useAnalysisStore()
    await store.parseImage(pngFile())

    expect(store.addImageHotspot()).toBeNull()

    expect(store.hotspots).toEqual([
      { text: '' },
      { text: '球场上的球拍特写', clue: expect.objectContaining({ why_it_works: ['反差感'] }) },
    ])
    expect(store.imagePhase).toBe('idle')
  })

  it('「用这条热点分析」只提交当前图片这一条，其余输入不动', async () => {
    vi.mocked(parseImageClue).mockResolvedValue(parsedResult())
    vi.mocked(submitAnalysis).mockResolvedValue(ACCEPTED)
    const store = useAnalysisStore()
    store.hotspots = [{ text: '手动热点' }]
    await store.parseImage(pngFile())

    await expect(store.submitOnly()).resolves.toBeNull()

    expect(submitAnalysis).toHaveBeenCalledWith({
      hotspots: ['球场上的球拍特写'],
      topk: 5,
      clues: [expect.objectContaining({ hotspot_raw: '球场上的球拍特写' })],
    })
    expect(store.hotspots).toEqual([{ text: '手动热点' }])
    store.reset()
  })

  it('没解析就先点「用这条热点分析」：给提示且不发请求', async () => {
    const store = useAnalysisStore()
    await expect(store.submitOnly()).resolves.toBe('请先解析图片')
    expect(submitAnalysis).not.toHaveBeenCalled()
  })
})
