import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiRequestError } from '@/api/client'
import type { JobStatus } from '@/api/analysis'
import { getJob, submitAnalysis } from '@/api/analysis'

/** 分析台的一次提交—轮询流程：idle → submitting → polling → succeeded / failed。 */
export type AnalysisPhase = 'idle' | 'submitting' | 'polling' | 'succeeded' | 'failed'

const POLL_INTERVAL_MS = 2000

export const useAnalysisStore = defineStore('analysis', () => {
  // 输入区：hotspots 用对象数组是为了适配 n-dynamic-input（原始类型数组无法双向绑定单个元素）
  const hotspots = ref<{ text: string }[]>([{ text: '' }])
  const topk = ref(5)

  const phase = ref<AnalysisPhase>('idle')
  const job = ref<JobStatus | null>(null)
  const failure = ref<{ message: string; detail?: string } | null>(null)
  /** 提交时刻（毫秒），用于耗时展示 */
  const startedAt = ref<number | null>(null)

  let timer: number | undefined

  const isActive = computed(() => phase.value === 'submitting' || phase.value === 'polling')

  function stopPolling(): void {
    if (timer !== undefined) {
      window.clearInterval(timer)
      timer = undefined
    }
  }

  function describeError(err: unknown): { message: string; detail?: string } {
    if (err instanceof ApiRequestError) {
      return { message: err.message, detail: err.detail === undefined ? undefined : JSON.stringify(err.detail) }
    }
    if (err instanceof Error) {
      return { message: err.message }
    }
    return { message: '未知错误' }
  }

  async function pollOnce(jobId: string): Promise<void> {
    try {
      const status = await getJob(jobId)
      job.value = status
      if (status.status === 'succeeded' || status.status === 'failed') {
        stopPolling()
        phase.value = status.status
        if (status.status === 'failed') {
          const err = status.error
          failure.value = err
            ? { message: err.message, detail: err.detail === undefined || err.detail === null ? undefined : JSON.stringify(err.detail) }
            : { message: '任务失败（后端未返回错误详情）' }
        }
      }
    } catch (err) {
      stopPolling()
      phase.value = 'failed'
      failure.value = describeError(err)
    }
  }

  function startPolling(jobId: string): void {
    stopPolling()
    timer = window.setInterval(() => {
      void pollOnce(jobId)
    }, POLL_INTERVAL_MS)
  }

  /** 提交前的基础校验（契约 AnalyzeRequest：1–10 条、每条 1–500 字、topk 1–20）。 */
  function validateInput(): string | null {
    const texts = hotspots.value.map((h) => h.text.trim()).filter((t) => t.length > 0)
    if (texts.length === 0) return '请至少填写一个热点'
    if (texts.length > 10) return '热点最多 10 个'
    if (texts.some((t) => t.length > 500)) return '单条热点不能超过 500 字'
    if (!Number.isInteger(topk.value) || topk.value < 1 || topk.value > 20) return '候选数 topk 需在 1–20 之间'
    return null
  }

  async function submit(): Promise<string | null> {
    const invalid = validateInput()
    if (invalid !== null) return invalid

    stopPolling()
    phase.value = 'submitting'
    job.value = null
    failure.value = null
    startedAt.value = Date.now()

    const payload = {
      hotspots: hotspots.value.map((h) => h.text.trim()).filter((t) => t.length > 0),
      topk: topk.value,
    }
    try {
      const accepted = await submitAnalysis(payload)
      job.value = { job_id: accepted.job_id, run_id: accepted.run_id, status: 'queued', progress: 0 }
      phase.value = 'polling'
      startPolling(accepted.job_id)
      return null
    } catch (err) {
      phase.value = 'failed'
      failure.value = describeError(err)
      return null
    }
  }

  function reset(): void {
    stopPolling()
    phase.value = 'idle'
    job.value = null
    failure.value = null
    startedAt.value = null
  }

  return { hotspots, topk, phase, job, failure, startedAt, isActive, submit, reset }
})
