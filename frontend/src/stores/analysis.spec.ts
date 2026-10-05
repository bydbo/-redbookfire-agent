import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { JobStatus } from '@/api/analysis'
import { getJob, submitAnalysis } from '@/api/analysis'
import { ApiRequestError } from '@/api/client'
import { useAnalysisStore } from '@/stores/analysis'

vi.mock('@/api/analysis', () => ({
  submitAnalysis: vi.fn(),
  getJob: vi.fn(),
}))

const POLL_MS = 2000
const ACCEPTED = { job_id: 'job-1', run_id: 'run-1' }

function job(status: JobStatus['status'], extra: Partial<JobStatus> = {}): JobStatus {
  return { job_id: 'job-1', run_id: 'run-1', status, progress: 0, ...extra }
}

describe('useAnalysisStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.mocked(submitAnalysis).mockReset()
    vi.mocked(getJob).mockReset()
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
